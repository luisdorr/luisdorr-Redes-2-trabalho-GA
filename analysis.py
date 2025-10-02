from __future__ import annotations

import dataclasses
import logging
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover
    plt = None  # type: ignore

_LOGGER = logging.getLogger(__name__)

PROTOCOLS: Sequence[str] = ("ospf_gaming", "ospf_frr")
COMPOSE_FILES: Dict[str, Path] = {
    "ospf_gaming": Path("docker-compose.yml"),
    "ospf_frr": Path("docker-compose.frr.yml"),
}
PING_SOURCE = "r1"
DESTINATION_IP = "10.0.35.3"
DEGRADED_ROUTER = "r3"
DEGRADED_INTERFACE = "eth0"
PING_COUNT = 15
PING_INTERVAL = 0.2
PING_TIMEOUT = 60
BOOTSTRAP_DELAY = 15
CONVERGENCE_TIMEOUT = 120

RESULTS_DIR = Path("results")
RAW_DIR = RESULTS_DIR / "raw"
TABLES_DIR = RESULTS_DIR / "tables"
FIGURES_DIR = RESULTS_DIR / "figures"

PING_LOSS = re.compile(r"(\d+(?:\.\d+)?)% packet loss")
PING_RTT = re.compile(
    r"(?:=|:)\s*(?P<min>\d+(?:\.\d+)?)/(?P<avg>\d+(?:\.\d+)?)/(?P<max>\d+(?:\.\d+)?)(?:/(?P<mdev>\d+(?:\.\d+)?))?"
)
PING_SAMPLE = re.compile(r"time=(\d+(?:\.\d+)?)\s*ms")
ROUTE_VIA = re.compile(r"via\s+(\d+(?:\.\d+){3})")


@dataclasses.dataclass(slots=True)
class PingMetrics:
    latency_ms: Optional[float]
    jitter_ms: Optional[float]
    loss_percent: Optional[float]
    raw_output: str


@dataclasses.dataclass(slots=True)
class ExperimentResult:
    protocol: str
    baseline: PingMetrics
    post: PingMetrics
    baseline_next_hop: Optional[str]
    post_next_hop: Optional[str]
    convergence_time_s: Optional[float]


def ensure_directories() -> None:
    for directory in (RESULTS_DIR, RAW_DIR, TABLES_DIR, FIGURES_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    for protocol in PROTOCOLS:
        (RAW_DIR / protocol).mkdir(parents=True, exist_ok=True)


def run_command(cmd: Sequence[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    quoted = " ".join(cmd)
    _LOGGER.debug("executing: %s", quoted)
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        _LOGGER.error("timeout after %ss: %s", timeout, quoted)
        return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", exc.stderr or "")
    except Exception:
        _LOGGER.exception("failed to run %s", quoted)
        return subprocess.CompletedProcess(cmd, 1, "", "execution failure")
    if completed.returncode != 0:
        _LOGGER.warning("command %s exited with %s", quoted, completed.returncode)
    return completed


def parse_ping(output: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    loss_match = PING_LOSS.search(output)
    rtt_match = PING_RTT.search(output)
    latency = float(rtt_match.group("avg")) if rtt_match else None
    jitter: Optional[float] = None
    if rtt_match:
        mdev = rtt_match.group("mdev")
        if mdev is not None:
            jitter = float(mdev)
    samples = [float(match.group(1)) for match in PING_SAMPLE.finditer(output)]
    if len(samples) >= 2:
        mean = sum(samples) / len(samples)
        jitter = math.sqrt(sum((sample - mean) ** 2 for sample in samples) / len(samples))
    loss = float(loss_match.group(1)) if loss_match else None
    return latency, jitter, loss


def extract_next_hop(route_output: str) -> Optional[str]:
    match = ROUTE_VIA.search(route_output)
    return match.group(1) if match else None


def save_raw(protocol: str, label: str, content: str) -> None:
    path = RAW_DIR / protocol / f"{label}.txt"
    path.write_text(content, encoding="utf-8")


class ExperimentRunner:
    def __init__(self, protocols: Iterable[str]) -> None:
        self.protocols = list(protocols)

    def run(self) -> List[ExperimentResult]:
        ensure_directories()
        results: List[ExperimentResult] = []
        for protocol in self.protocols:
            try:
                results.append(self._run_protocol(protocol))
            except Exception:
                _LOGGER.exception("protocol %s failed", protocol)
        if results:
            self._export_tables(results)
            self._generate_figures(results)
        return results

    def _run_protocol(self, protocol: str) -> ExperimentResult:
        compose_file = COMPOSE_FILES[protocol]
        self._start_topology(compose_file)
        time.sleep(BOOTSTRAP_DELAY)

        baseline = self._collect_ping(protocol, "baseline")
        baseline_route_output = self._route_snapshot()
        baseline_next_hop = extract_next_hop(baseline_route_output)
        save_raw(protocol, "route_baseline", baseline_route_output)

        self._apply_degradation()
        convergence_time = self._measure_convergence(baseline_next_hop)
        post = self._collect_ping(protocol, "post_degradation")
        post_route_output = self._route_snapshot()
        post_next_hop = extract_next_hop(post_route_output)
        save_raw(protocol, "route_post", post_route_output)

        self._clear_degradation()
        self._stop_topology(compose_file)

        return ExperimentResult(
            protocol=protocol,
            baseline=baseline,
            post=post,
            baseline_next_hop=baseline_next_hop,
            post_next_hop=post_next_hop,
            convergence_time_s=convergence_time,
        )

    def _start_topology(self, compose_file: Path) -> None:
        run_command(["docker", "compose", "-f", str(compose_file), "up", "-d"], timeout=120)

    def _stop_topology(self, compose_file: Path) -> None:
        run_command(["docker", "compose", "-f", str(compose_file), "down", "-v"], timeout=120)

    def _collect_ping(self, protocol: str, label: str) -> PingMetrics:
        cmd = [
            "docker",
            "exec",
            PING_SOURCE,
            "ping",
            "-c",
            str(PING_COUNT),
            "-i",
            str(PING_INTERVAL),
            DESTINATION_IP,
        ]
        completed = run_command(cmd, timeout=PING_TIMEOUT)
        output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        save_raw(protocol, f"ping_{label}", output)
        latency, jitter, loss = parse_ping(output)
        return PingMetrics(latency_ms=latency, jitter_ms=jitter, loss_percent=loss, raw_output=output)

    def _route_snapshot(self) -> str:
        cmd = ["docker", "exec", PING_SOURCE, "ip", "route", "get", DESTINATION_IP]
        completed = run_command(cmd, timeout=30)
        return (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")

    def _apply_degradation(self) -> None:
        cmd = [
            "docker",
            "exec",
            DEGRADED_ROUTER,
            "tc",
            "qdisc",
            "replace",
            "dev",
            DEGRADED_INTERFACE,
            "root",
            "netem",
            "delay",
            "200ms",
            "40ms",
            "distribution",
            "normal",
            "loss",
            "20%",
        ]
        run_command(cmd, timeout=30)

    def _clear_degradation(self) -> None:
        cmd = [
            "docker",
            "exec",
            DEGRADED_ROUTER,
            "tc",
            "qdisc",
            "del",
            "dev",
            DEGRADED_INTERFACE,
            "root",
        ]
        run_command(cmd, timeout=30)

    def _measure_convergence(self, baseline_next_hop: Optional[str]) -> Optional[float]:
        if not baseline_next_hop:
            return None
        start = time.time()
        deadline = start + CONVERGENCE_TIMEOUT
        while time.time() < deadline:
            route_output = self._route_snapshot()
            current = extract_next_hop(route_output)
            if current and current != baseline_next_hop:
                return round(time.time() - start, 3)
            time.sleep(1.0)
        return None

    def _export_tables(self, results: Sequence[ExperimentResult]) -> None:
        latency_rows = []
        for result in results:
            latency_rows.append(
                {
                    "protocol": result.protocol,
                    "phase": "baseline",
                    "latency_ms": result.baseline.latency_ms,
                    "jitter_ms": result.baseline.jitter_ms,
                    "loss_percent": result.baseline.loss_percent,
                    "next_hop": result.baseline_next_hop,
                }
            )
            latency_rows.append(
                {
                    "protocol": result.protocol,
                    "phase": "post_degradation",
                    "latency_ms": result.post.latency_ms,
                    "jitter_ms": result.post.jitter_ms,
                    "loss_percent": result.post.loss_percent,
                    "next_hop": result.post_next_hop,
                }
            )
        pd.DataFrame(latency_rows).to_csv(TABLES_DIR / "latency_jitter_loss.csv", index=False)

        convergence_rows = [
            {
                "protocol": result.protocol,
                "convergence_time_s": result.convergence_time_s,
            }
            for result in results
        ]
        pd.DataFrame(convergence_rows).to_csv(TABLES_DIR / "convergence.csv", index=False)

    def _generate_figures(self, results: Sequence[ExperimentResult]) -> None:
        if plt is None or not results:
            return

        protocols = [result.protocol for result in results]
        baseline_latency = [result.baseline.latency_ms or math.nan for result in results]
        post_latency = [result.post.latency_ms or math.nan for result in results]
        baseline_jitter = [result.baseline.jitter_ms or math.nan for result in results]
        post_jitter = [result.post.jitter_ms or math.nan for result in results]

        fig, ax = plt.subplots(figsize=(6, 4))
        width = 0.3
        positions = range(len(protocols))
        ax.bar([p - width for p in positions], baseline_latency, width=width, label="latency baseline")
        ax.bar(positions, post_latency, width=width, label="latency post")
        ax.bar([p + width for p in positions], post_jitter, width=width, label="jitter post")
        ax.set_xticks(list(positions))
        ax.set_xticklabels(protocols)
        ax.set_ylabel("milliseconds")
        ax.set_title("QoS metrics r1->r5")
        ax.legend()
        fig.tight_layout()
        fig.savefig(FIGURES_DIR / "latency_jitter.png")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(4, 4))
        ax.bar(protocols, [result.convergence_time_s or 0.0 for result in results], color="steelblue")
        ax.set_ylabel("seconds")
        ax.set_title("Convergence after r3 degradation")
        fig.tight_layout()
        fig.savefig(FIGURES_DIR / "convergence.png")
        plt.close(fig)


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    runner = ExperimentRunner(PROTOCOLS)
    runner.run()


if __name__ == "__main__":  # pragma: no cover
    main()


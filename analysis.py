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
DEGRADED_LINK_IP = "10.0.35.2"
PING_COUNT = 15
PING_INTERVAL = 0.2
PING_TIMEOUT = 60
BOOTSTRAP_DELAY = 15
CONVERGENCE_TIMEOUT = 60
WARMUP_TIMEOUT = 60
WARMUP_PROBE_COUNT = 4
RETRY_PAUSE = 2.0
MEASUREMENT_TIMEOUT = 60

RESULTS_DIR = Path("results")
RAW_DIR = RESULTS_DIR / "raw"
TABLES_DIR = RESULTS_DIR / "tables"

PING_LOSS = re.compile(r"(\d+(?:\.\d+)?)% packet loss")
PING_RTT = re.compile(
    r"(?:=|:)\s*(?P<min>\d+(?:\.\d+)?)/(?P<avg>\d+(?:\.\d+)?)/(?P<max>\d+(?:\.\d+)?)(?:/(?P<mdev>\d+(?:\.\d+)?))?"
)
PING_SAMPLE = re.compile(r"time=(\d+(?:\.\d+)?)\s*ms")
ROUTE_VIA = re.compile(r"via\s+(\d+(?:\.\d+){3})")


@dataclasses.dataclass(slots=True)
class PingMetrics:
    latency_ms: float
    jitter_ms: float
    loss_percent: float
    raw_output: str


@dataclasses.dataclass(slots=True)
class ExperimentResult:
    protocol: str
    baseline: PingMetrics
    post: PingMetrics
    baseline_next_hop: Optional[str]
    post_next_hop: Optional[str]
    convergence_time_s: float


def ensure_directories() -> None:
    for directory in (RESULTS_DIR, RAW_DIR, TABLES_DIR):
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


def parse_ping(output: str) -> tuple[float, float, float]:
    loss_match = PING_LOSS.search(output)
    rtt_match = PING_RTT.search(output)
    latency = float(rtt_match.group("avg")) if rtt_match else math.nan
    jitter = math.nan
    if rtt_match:
        mdev = rtt_match.group("mdev")
        if mdev is not None:
            jitter = float(mdev)
    samples = [float(match.group(1)) for match in PING_SAMPLE.finditer(output)]
    if len(samples) >= 2:
        mean = sum(samples) / len(samples)
        jitter = math.sqrt(sum((sample - mean) ** 2 for sample in samples) / len(samples))
    if math.isnan(jitter) and len(samples) == 1:
        jitter = 0.0
    loss = float(loss_match.group(1)) if loss_match else math.nan
    return latency, jitter, loss


def extract_next_hop(route_output: str) -> Optional[str]:
    match = ROUTE_VIA.search(route_output)
    return match.group(1) if match else None


def save_raw(protocol: str, label: str, content: str) -> None:
    path = RAW_DIR / protocol / f"{label}.txt"
    path.write_text(content, encoding="utf-8")


def _is_usable(loss_percent: float) -> bool:
    return not math.isnan(loss_percent) and loss_percent < 100.0


def _change(baseline: float, post: float) -> float:
    if math.isnan(baseline) or math.isnan(post):
        return float('nan')
    return post - baseline


def _gain(baseline: float, post: float) -> float:
    if math.isnan(baseline) or math.isnan(post):
        return float('nan')
    return baseline - post


def _composite_score(metrics: PingMetrics) -> float:
    values = (metrics.latency_ms, metrics.jitter_ms, metrics.loss_percent)
    if any(math.isnan(value) for value in values):
        return float('nan')
    return sum(values)


class ExperimentRunner:
    def __init__(self, protocols: Iterable[str]) -> None:
        self.protocols = list(protocols)
        self._active_degraded_interface: Optional[str] = None

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
        return results

    def _run_protocol(self, protocol: str) -> ExperimentResult:
        compose_file = COMPOSE_FILES[protocol]
        self._start_topology(compose_file)
        time.sleep(BOOTSTRAP_DELAY)
        self._warmup_path()
        self._active_degraded_interface = self._detect_degraded_interface() or DEGRADED_INTERFACE

        baseline_route_output = self._await_route_snapshot()
        baseline_next_hop = extract_next_hop(baseline_route_output)
        save_raw(protocol, "route_baseline", baseline_route_output)

        baseline = self._measure_ping(protocol, "baseline")

        self._apply_degradation()
        convergence_time = self._measure_convergence(baseline_next_hop)
        post = self._measure_ping(protocol, "post_degradation")
        post_route_output = self._route_snapshot()
        post_next_hop = extract_next_hop(post_route_output)
        save_raw(protocol, "route_post", post_route_output)

        self._clear_degradation()
        self._stop_topology(compose_file)
        self._active_degraded_interface = None

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

    def _warmup_path(self) -> None:
        deadline = time.time() + WARMUP_TIMEOUT
        while time.time() < deadline:
            metrics = self._collect_ping(count=WARMUP_PROBE_COUNT)
            if _is_usable(metrics.loss_percent):
                return
            time.sleep(RETRY_PAUSE)

    def _measure_ping(self, protocol: str, label: str) -> PingMetrics:
        deadline = time.time() + MEASUREMENT_TIMEOUT
        metrics = self._collect_ping()
        if _is_usable(metrics.loss_percent):
            save_raw(protocol, label, metrics.raw_output)
            return metrics
        while time.time() < deadline:
            time.sleep(RETRY_PAUSE)
            metrics = self._collect_ping()
            if _is_usable(metrics.loss_percent):
                save_raw(protocol, label, metrics.raw_output)
                return metrics
        save_raw(protocol, label, metrics.raw_output)
        return metrics

    def _collect_ping(self, *, count: Optional[int] = None) -> PingMetrics:
        ping_count = count if count is not None else PING_COUNT
        cmd = [
            "docker",
            "exec",
            PING_SOURCE,
            "ping",
            "-c",
            str(ping_count),
            "-i",
            str(PING_INTERVAL),
            DESTINATION_IP,
        ]
        completed = run_command(cmd, timeout=PING_TIMEOUT)
        output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        latency, jitter, loss = parse_ping(output)
        return PingMetrics(latency_ms=latency, jitter_ms=jitter, loss_percent=loss, raw_output=output)

    def _detect_degraded_interface(self) -> Optional[str]:
        cmd = ["docker", "exec", DEGRADED_ROUTER, "ip", "-o", "addr", "show", "scope", "global"]
        completed = run_command(cmd, timeout=30)
        output = completed.stdout or ""
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            interface = parts[1]
            if '@' in interface:
                interface = interface.split('@', 1)[0]
            address = parts[3]
            if address.split('/', 1)[0] == DEGRADED_LINK_IP:
                return interface
        return None

    def _await_route_snapshot(self) -> str:
        deadline = time.time() + WARMUP_TIMEOUT
        snapshot = ""
        while time.time() < deadline:
            snapshot = self._route_snapshot()
            if extract_next_hop(snapshot):
                return snapshot
            time.sleep(RETRY_PAUSE)
        return snapshot

    def _route_snapshot(self) -> str:
        cmd = ["docker", "exec", PING_SOURCE, "ip", "route", "get", DESTINATION_IP]
        completed = run_command(cmd, timeout=30)
        return (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")

    def _apply_degradation(self) -> None:
        interface = self._active_degraded_interface or DEGRADED_INTERFACE
        cmd = [
            "docker",
            "exec",
            DEGRADED_ROUTER,
            "tc",
            "qdisc",
            "replace",
            "dev",
            interface,
            "root",
            "netem",
            "delay",
            "300ms",
            "80ms",
            "distribution",
            "normal",
            "loss",
            "35%",
        ]
        run_command(cmd, timeout=30)

    def _clear_degradation(self) -> None:
        interface = self._active_degraded_interface or DEGRADED_INTERFACE
        cmd = [
            "docker",
            "exec",
            DEGRADED_ROUTER,
            "tc",
            "qdisc",
            "del",
            "dev",
            interface,
            "root",
        ]
        run_command(cmd, timeout=30)

    def _measure_convergence(self, baseline_next_hop: Optional[str]) -> float:
        if not baseline_next_hop:
            return float("nan")
        start = time.time()
        deadline = start + CONVERGENCE_TIMEOUT
        while time.time() < deadline:
            route_output = self._route_snapshot()
            current = extract_next_hop(route_output)
            if current and current != baseline_next_hop:
                return round(time.time() - start, 3)
            time.sleep(1.0)
        return float(CONVERGENCE_TIMEOUT)

    def _export_tables(self, results: Sequence[ExperimentResult]) -> None:
        latency_rows = []
        comparison_rows = []
        for result in results:
            latency_rows.append(
                {
                    "protocol": result.protocol,
                    "phase": "baseline",
                    "latency_ms": result.baseline.latency_ms,
                    "jitter_ms": result.baseline.jitter_ms,
                    "loss_percent": result.baseline.loss_percent,
                    "next_hop": result.baseline_next_hop or "",
                }
            )
            latency_rows.append(
                {
                    "protocol": result.protocol,
                    "phase": "post_degradation",
                    "latency_ms": result.post.latency_ms,
                    "jitter_ms": result.post.jitter_ms,
                    "loss_percent": result.post.loss_percent,
                    "next_hop": result.post_next_hop or "",
                }
            )
            baseline_score = _composite_score(result.baseline)
            post_score = _composite_score(result.post)
            comparison_rows.append(
                {
                    "protocol": result.protocol,
                    "baseline_latency_ms": result.baseline.latency_ms,
                    "post_latency_ms": result.post.latency_ms,
                    "latency_change_ms": _change(result.baseline.latency_ms, result.post.latency_ms),
                    "latency_improvement_ms": _gain(result.baseline.latency_ms, result.post.latency_ms),
                    "baseline_jitter_ms": result.baseline.jitter_ms,
                    "post_jitter_ms": result.post.jitter_ms,
                    "jitter_change_ms": _change(result.baseline.jitter_ms, result.post.jitter_ms),
                    "jitter_improvement_ms": _gain(result.baseline.jitter_ms, result.post.jitter_ms),
                    "baseline_loss_percent": result.baseline.loss_percent,
                    "post_loss_percent": result.post.loss_percent,
                    "loss_change_percent": _change(result.baseline.loss_percent, result.post.loss_percent),
                    "loss_improvement_percent": _gain(result.baseline.loss_percent, result.post.loss_percent),
                    "baseline_quality_index": baseline_score,
                    "post_quality_index": post_score,
                    "quality_gain": _gain(baseline_score, post_score),
                    "convergence_time_s": result.convergence_time_s,
                }
            )
        columns = ['protocol', 'phase', 'latency_ms', 'jitter_ms', 'loss_percent', 'next_hop']
        ordered_rows = [{col: row.get(col, '') for col in columns} for row in latency_rows]
        latency_df = pd.DataFrame(ordered_rows)
        latency_df.to_csv(
            TABLES_DIR / "latency_jitter_loss.csv",
            index=False,
        )

        comparison_df = pd.DataFrame(comparison_rows)
        comparison_df.to_csv(
            TABLES_DIR / "comparison.csv",
            index=False,
        )

        convergence_df = pd.DataFrame(
            {
                "protocol": [r.protocol for r in results],
                "convergence_time_s": [r.convergence_time_s for r in results],
            }
        )
        convergence_df.to_csv(
            TABLES_DIR / "convergence.csv",
            index=False,
        )


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    runner = ExperimentRunner(PROTOCOLS)
    runner.run()


if __name__ == "__main__":  # pragma: no cover
    main()

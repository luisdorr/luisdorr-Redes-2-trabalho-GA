from __future__ import annotations

import contextlib
import dataclasses
import logging
import math
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover - matplotlib might be missing in CI
    plt = None  # type: ignore

import pandas as pd

_LOGGER = logging.getLogger(__name__)

PING_LOSS_RE = re.compile(r"(\d+(?:\.\d+)?)% packet loss")
PING_RTT_RE = re.compile(
    r"(?:rtt|round-trip) min/avg/max(?:/mdev)? = "
    r"(?P<min>\d+(?:\.\d+)?)/(?P<avg>\d+(?:\.\d+)?)/(?P<max>\d+(?:\.\d+)?)(?:/(?P<mdev>\d+(?:\.\d+)?))?"
)
TRACEROUTE_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PACKETS_CAPTURED_RE = re.compile(r"(\d+) packets captured")

PROTOCOLS: Sequence[str] = ("ospf_gaming", "ospf_frr")
ROUTERS: Sequence[str] = tuple(f"r{i}" for i in range(1, 9))
DESTINATION_IP = "10.0.35.3"
PING_SOURCE = "r1"
DEGRADED_ROUTER = "r3"
DEGRADED_INTERFACE = "eth0"

RESULTS_DIR = Path("results")
RAW_DIR = RESULTS_DIR / "raw"
TABLES_DIR = RESULTS_DIR / "tables"
FIGURES_DIR = RESULTS_DIR / "figures"

COMPOSE_FILES: Dict[str, Path] = {
    "ospf_gaming": Path("docker-compose.yml"),
    "ospf_frr": Path("docker-compose.frr.yml"),
}


@dataclasses.dataclass
class PingMetrics:
    """Metrics extracted from a ping invocation."""

    latency_ms: Optional[float]
    jitter_ms: Optional[float]
    loss_percent: Optional[float]
    raw_output: str


@dataclasses.dataclass
class ExperimentResult:
    """Aggregation of all metrics gathered for a protocol."""

    protocol: str
    baseline_ping: PingMetrics
    post_ping: PingMetrics
    convergence_time: Optional[float]
    routing_overhead: int


def ensure_results_dirs() -> None:
    """Create the directory structure used to store artefacts."""

    for directory in (RESULTS_DIR, RAW_DIR, TABLES_DIR, FIGURES_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    for protocol in PROTOCOLS:
        (RAW_DIR / protocol).mkdir(parents=True, exist_ok=True)


def run_command(cmd: Sequence[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Execute *cmd* returning a ``CompletedProcess`` instance.

    The helper never raises, returning the completed process even for non-zero
    exit codes so callers can decide how to proceed.
    """

    quoted = " ".join(shlex.quote(str(part)) for part in cmd)
    _LOGGER.debug("Executing command: %s", quoted)
    try:
        completed = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        _LOGGER.error("Command timed out after %s seconds: %s", timeout, quoted)
        return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", exc.stderr or "")
    except FileNotFoundError as exc:  # pragma: no cover - defensive
        _LOGGER.error("Command not found: %s", exc)
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        _LOGGER.exception("Failed to execute command %s", quoted)
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        if stderr:
            _LOGGER.warning("Command %s returned %s: %s", quoted, completed.returncode, stderr)
        else:
            _LOGGER.warning("Command %s returned %s", quoted, completed.returncode)
    return completed


def parse_ping_output(output: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Extract latency, jitter and loss percentage from ping *output*."""

    loss_match = PING_LOSS_RE.search(output)
    rtt_match = PING_RTT_RE.search(output)

    latency = float(rtt_match.group("avg")) if rtt_match else None
    jitter: Optional[float] = None
    if rtt_match:
        mdev = rtt_match.group("mdev")
        if mdev is not None:
            jitter = float(mdev)
        else:
            try:
                min_rtt = float(rtt_match.group("min"))
                max_rtt = float(rtt_match.group("max"))
                jitter = max_rtt - min_rtt
            except (TypeError, ValueError):  # pragma: no cover - defensive
                jitter = None
    loss = float(loss_match.group(1)) if loss_match else None
    return latency, jitter, loss


def parse_traceroute_output(output: str) -> List[str]:
    """Return the ordered list of hops from traceroute *output*."""

    hops: List[str] = []
    for line in output.splitlines():
        if line.startswith("traceroute"):
            continue
        match = TRACEROUTE_IP_RE.search(line)
        if match:
            hops.append(match.group(0))
    return hops


class ExperimentRunner:
    """High level orchestrator for the comparative analysis."""

    def __init__(
        self,
        *,
        ping_count: int = 10,
        ping_timeout: int = 60,
        traceroute_timeout: int = 60,
        tcpdump_timeout: int = 120,
        tcpdump_count: int = 200,
        convergence_timeout: float = 120.0,
        poll_interval: float = 2.0,
        startup_delay: float = 10.0,
        protocols: Sequence[str] | None = None,
        routers: Sequence[str] | None = None,
        command_runner: Callable[[Sequence[str], int], subprocess.CompletedProcess[str]] = run_command,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ping_count = ping_count
        self.ping_timeout = ping_timeout
        self.traceroute_timeout = traceroute_timeout
        self.tcpdump_timeout = tcpdump_timeout
        self.tcpdump_count = tcpdump_count
        self.convergence_timeout = convergence_timeout
        self.poll_interval = poll_interval
        self.startup_delay = startup_delay
        self.protocols = list(protocols) if protocols is not None else list(PROTOCOLS)
        self.routers = list(routers) if routers is not None else list(ROUTERS)
        self.command_runner = command_runner
        self.sleep = sleep_fn

    # ------------------------------------------------------------------
    # Topology management
    # ------------------------------------------------------------------
    def start_topology(self, protocol: str) -> None:
        compose = COMPOSE_FILES.get(protocol)
        if compose is None:
            raise ValueError(f"Unknown protocol {protocol}")
        cmd = ["docker-compose", "-f", str(compose), "up", "-d", "--remove-orphans"]
        self.command_runner(cmd, timeout=self.tcpdump_timeout)
        if self.startup_delay > 0:
            self.sleep(self.startup_delay)

    def stop_topology(self, protocol: str) -> None:
        compose = COMPOSE_FILES.get(protocol)
        if compose is None:
            return
        cmd = ["docker-compose", "-f", str(compose), "down"]
        self.command_runner(cmd, timeout=self.tcpdump_timeout)

    # ------------------------------------------------------------------
    # Measurement primitives
    # ------------------------------------------------------------------
    def run_ping(self, protocol: str, phase: str) -> PingMetrics:
        cmd = ["docker", "exec", PING_SOURCE, "ping", "-c", str(self.ping_count), DESTINATION_IP]
        completed = self.command_runner(cmd, timeout=self.ping_timeout)
        raw_text = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        save_path = RAW_DIR / protocol / f"{phase}_ping.txt"
        save_path.write_text(raw_text, encoding="utf-8")
        latency, jitter, loss = parse_ping_output(completed.stdout)
        return PingMetrics(latency, jitter, loss, raw_text)

    def collect_path(self, protocol: str, phase: str, *, save: bool = True) -> List[str]:
        cmd = ["docker", "exec", PING_SOURCE, "traceroute", "-n", DESTINATION_IP]
        completed = self.command_runner(cmd, timeout=self.traceroute_timeout)
        raw_text = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        if save:
            save_path = RAW_DIR / protocol / f"{phase}_traceroute.txt"
            save_path.write_text(raw_text, encoding="utf-8")
        hops = parse_traceroute_output(raw_text)
        if not hops:
            fallback_cmd = ["docker", "exec", PING_SOURCE, "ip", "route", "get", DESTINATION_IP]
            fallback = self.command_runner(fallback_cmd, timeout=self.traceroute_timeout)
            raw = (fallback.stdout or "") + ("\n" + fallback.stderr if fallback.stderr else "")
            if save:
                save_path = RAW_DIR / protocol / f"{phase}_route.txt"
                save_path.write_text(raw, encoding="utf-8")
            hops = parse_traceroute_output(raw)
        return hops

    def apply_degradation(self, protocol: str) -> None:
        log_path = RAW_DIR / protocol / "degradation.log"
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
            "120ms",
            "40ms",
            "loss",
            "5%",
        ]
        completed = self.command_runner(cmd, timeout=self.tcpdump_timeout)
        log_path.write_text(
            (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else ""),
            encoding="utf-8",
        )

    def clear_degradation(self) -> None:
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
        self.command_runner(cmd, timeout=self.tcpdump_timeout)

    def measure_convergence(self, baseline_path: List[str]) -> Optional[float]:
        start = time.time()
        deadline = start + self.convergence_timeout
        while time.time() < deadline:
            cmd = ["docker", "exec", PING_SOURCE, "traceroute", "-n", DESTINATION_IP]
            completed = self.command_runner(cmd, timeout=self.traceroute_timeout)
            hops = parse_traceroute_output((completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else ""))
            if hops and baseline_path and hops != baseline_path:
                now = time.time()
                return max(0.0, now - start)
            self.sleep(self.poll_interval)
        return None

    def capture_routing_packets(self, protocol: str, phase: str) -> int:
        total_packets = 0
        for router in self.routers:
            pcap_path = RAW_DIR / protocol / f"{phase}_{router}.pcap"
            cmd = [
                "docker",
                "exec",
                router,
                "tcpdump",
                "-i",
                "any",
                "-nn",
                "-c",
                str(self.tcpdump_count),
                "-w",
                str(pcap_path),
                "ospf",
            ]
            completed = self.command_runner(cmd, timeout=self.tcpdump_timeout)
            text = (completed.stdout or "") + "\n" + (completed.stderr or "")
            match = PACKETS_CAPTURED_RE.search(text)
            if match:
                total_packets += int(match.group(1))
        return total_packets

    # ------------------------------------------------------------------
    # Experiment execution
    # ------------------------------------------------------------------
    def run_protocol(self, protocol: str) -> ExperimentResult:
        ensure_results_dirs()
        self.start_topology(protocol)
        baseline_path: List[str] = []
        baseline_ping: Optional[PingMetrics] = None
        post_ping: Optional[PingMetrics] = None
        convergence_time: Optional[float] = None
        routing_overhead = 0
        try:
            baseline_path = self.collect_path(protocol, "baseline", save=True)
            baseline_ping = self.run_ping(protocol, "baseline")
            self.apply_degradation(protocol)
            convergence_time = self.measure_convergence(baseline_path)
            post_ping = self.run_ping(protocol, "post_degradation")
            routing_overhead = self.capture_routing_packets(protocol, "routing")
        finally:
            with contextlib.suppress(Exception):  # pragma: no cover - cleanup
                self.clear_degradation()
            self.stop_topology(protocol)

        if baseline_ping is None or post_ping is None:  # pragma: no cover - defensive
            raise RuntimeError("Ping metrics missing")

        return ExperimentResult(
            protocol=protocol,
            baseline_ping=baseline_ping,
            post_ping=post_ping,
            convergence_time=convergence_time,
            routing_overhead=routing_overhead,
        )

    def run(self, protocols: Optional[Iterable[str]] = None) -> List[ExperimentResult]:
        ensure_results_dirs()
        selected = list(protocols) if protocols is not None else self.protocols
        results: List[ExperimentResult] = []
        for protocol in selected:
            try:
                results.append(self.run_protocol(protocol))
            except Exception:
                _LOGGER.exception("Failed to execute protocol %s", protocol)
        if results:
            self.export_tables(results)
            self.generate_figures(results)
        return results

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------
    def export_tables(self, results: Sequence[ExperimentResult]) -> None:
        latency_rows = []
        for result in results:
            latency_rows.append(
                {
                    "protocol": result.protocol,
                    "phase": "baseline",
                    "latency_ms": result.baseline_ping.latency_ms,
                    "jitter_ms": result.baseline_ping.jitter_ms,
                    "loss_percent": result.baseline_ping.loss_percent,
                }
            )
            latency_rows.append(
                {
                    "protocol": result.protocol,
                    "phase": "post_degradation",
                    "latency_ms": result.post_ping.latency_ms,
                    "jitter_ms": result.post_ping.jitter_ms,
                    "loss_percent": result.post_ping.loss_percent,
                }
            )
        latency_df = pd.DataFrame(latency_rows)
        latency_df.to_csv(TABLES_DIR / "latency_jitter.csv", index=False)

        convergence_df = pd.DataFrame(
            {
                "protocol": [r.protocol for r in results],
                "convergence_time_s": [r.convergence_time for r in results],
            }
        )
        convergence_df.to_csv(TABLES_DIR / "convergence_time.csv", index=False)

        overhead_df = pd.DataFrame(
            {
                "protocol": [r.protocol for r in results],
                "routing_packets": [r.routing_overhead for r in results],
            }
        )
        overhead_df.to_csv(TABLES_DIR / "routing_overhead.csv", index=False)

    def generate_figures(self, results: Sequence[ExperimentResult]) -> None:
        if plt is None:  # pragma: no cover - matplotlib optional
            _LOGGER.warning("matplotlib is not available; skipping figures")
            return

        protocols = [r.protocol for r in results]

        # Convergence time bar chart
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar(protocols, [r.convergence_time or 0.0 for r in results], color="steelblue")
        ax.set_ylabel("Seconds")
        ax.set_title("Convergence Time")
        fig.tight_layout()
        fig.savefig(FIGURES_DIR / "convergence_time.png")
        plt.close(fig)

        # Latency and jitter comparison
        baseline_latency = [r.baseline_ping.latency_ms or math.nan for r in results]
        post_latency = [r.post_ping.latency_ms or math.nan for r in results]
        baseline_jitter = [r.baseline_ping.jitter_ms or math.nan for r in results]
        post_jitter = [r.post_ping.jitter_ms or math.nan for r in results]

        width = 0.2
        x_positions = list(range(len(protocols)))

        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar([x - width for x in x_positions], baseline_latency, width=width, label="Latency (baseline)")
        ax.bar(x_positions, post_latency, width=width, label="Latency (post)")
        ax.bar([x + width for x in x_positions], baseline_jitter, width=width, label="Jitter (baseline)")
        ax.bar([x + 2 * width for x in x_positions], post_jitter, width=width, label="Jitter (post)")
        ax.set_xticks(x_positions)
        ax.set_xticklabels(protocols)
        ax.set_ylabel("Milliseconds")
        ax.set_title("Latency and Jitter")
        ax.legend()
        fig.tight_layout()
        fig.savefig(FIGURES_DIR / "latency_jitter.png")
        plt.close(fig)

        # Routing overhead comparison
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar(protocols, [r.routing_overhead for r in results], color="darkorange")
        ax.set_ylabel("Packets")
        ax.set_title("Routing Overhead")
        fig.tight_layout()
        fig.savefig(FIGURES_DIR / "routing_overhead.png")
        plt.close(fig)


def main() -> None:  # pragma: no cover - manual execution
    logging.basicConfig(level=logging.INFO)
    runner = ExperimentRunner()
    runner.run()


if __name__ == "__main__":  # pragma: no cover
    main()

from __future__ import annotations

import contextlib
import csv
import json
import logging
import math
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  
except ImportError:  
    plt = None  

_LOGGER = logging.getLogger(__name__)

PROTOCOLS: Sequence[str] = ("ospf_native", "ospf_gaming")
ROUTERS: Sequence[str] = tuple(f"r{i}" for i in range(1, 9))
HOSTS: Sequence[str] = ("h1", "h2")
RESULTS_DIR = Path("results")
RAW_DIR = RESULTS_DIR / "raw"

PING_LOSS_RE = re.compile(r"(\d+(?:\.\d+)?)% packet loss")
PING_RTT_RE = re.compile(
    r"(?:rtt|round-trip) min/avg/max(?:/mdev)? = "
    r"(?P<min>\d+(?:\.\d+)?)/(?P<avg>\d+(?:\.\d+)?)/(?P<max>\d+(?:\.\d+)?)(?:/(?P<mdev>\d+(?:\.\d+)?))?"
)
IP_REGEX = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
CONTROL_KEYWORDS = ("hello", "lsa", "dd", "ls_ack", "ls_req")


def ensure_results_dirs() -> None:
    """Create the directory structure used to store raw artefacts."""

    RESULTS_DIR.mkdir(exist_ok=True)
    RAW_DIR.mkdir(exist_ok=True)
    for protocol in PROTOCOLS:
        (RAW_DIR / protocol).mkdir(exist_ok=True)


def run_command(cmd: Sequence[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Execute a shell command returning the completed process.

    The function never raises and instead returns a ``CompletedProcess`` instance
    with ``stdout``/``stderr`` populated accordingly.
    """

    quoted = " ".join(shlex.quote(str(part)) for part in cmd)
    _LOGGER.debug("Running command: %s", quoted)
    try:
        completed = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:  
        _LOGGER.error("Command timed out after %s seconds: %s", timeout, quoted)
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return subprocess.CompletedProcess(cmd, returncode=124, stdout=stdout, stderr=stderr)
    except FileNotFoundError as exc:  
        _LOGGER.error("Command not found: %s", exc)
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr=str(exc))
    except Exception as exc:  
        _LOGGER.exception("Failed to execute command %s", quoted)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=str(exc))

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        if stderr:
            _LOGGER.warning("Command %s returned %s: %s", quoted, completed.returncode, stderr)
        else:
            _LOGGER.warning("Command %s returned %s", quoted, completed.returncode)

    return completed


def save_raw(protocol: str, name: str, content: str, extension: str = "txt") -> None:
    """Persist raw artefacts for later inspection."""

    directory = RAW_DIR / protocol
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.{extension}"
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:  
        _LOGGER.error("Unable to write artefact %s: %s", path, exc)


def parse_ping_output(output: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Extract latency, jitter and loss from a ping summary."""

    loss_match = PING_LOSS_RE.search(output)
    rtt_match = PING_RTT_RE.search(output)

    packet_loss = float(loss_match.group(1)) if loss_match else None
    avg_latency: Optional[float]
    jitter: Optional[float]

    avg_latency = None
    jitter = None

    if rtt_match:
        try:
            min_rtt = float(rtt_match.group("min"))
            avg_latency = float(rtt_match.group("avg"))
            max_rtt = float(rtt_match.group("max"))
            mdev_str = rtt_match.group("mdev")
            if mdev_str is not None:
                jitter = float(mdev_str)
            elif math.isfinite(min_rtt) and math.isfinite(max_rtt):
                jitter = max_rtt - min_rtt
        except (TypeError, ValueError):  
            avg_latency = None
            jitter = None

    return avg_latency, jitter, packet_loss


def parse_iperf_json(output: str) -> Optional[float]:
    """Parse iperf3 JSON output and return throughput in Mbps."""

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None

    end_section = data.get("end", {}) if isinstance(data, dict) else {}
    summary = None
    if isinstance(end_section, dict):
        summary = end_section.get("sum_received") or end_section.get("sum_sent")

    if isinstance(summary, dict):
        bps = summary.get("bits_per_second")
        if isinstance(bps, (int, float)):
            return bps / 1_000_000

    intervals = data.get("intervals") if isinstance(data, dict) else None
    if isinstance(intervals, list) and intervals:
        last = intervals[-1]
        if isinstance(last, dict):
            summary = last.get("sum")
            if isinstance(summary, dict):
                bps = summary.get("bits_per_second")
                if isinstance(bps, (int, float)):
                    return bps / 1_000_000

    return None


def parse_iperf_text(output: str) -> Optional[float]:
    """Parse human-readable iperf output and derive throughput in Mbps."""

    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:Mbits|Mbit)/sec", output)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:Kbits|Kbit)/sec", output)
    if match:
        return float(match.group(1)) / 1000.0
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:Gbits|Gbit)/sec", output)
    if match:
        return float(match.group(1)) * 1000.0
    return None


def parse_traceroute_output(output: str) -> List[str]:
    """Derive the hop sequence from traceroute output."""

    path: List[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("traceroute"):
            continue
        match = IP_REGEX.search(line)
        if match:
            path.append(match.group(0))
    return path


def parse_ip_route_get(output: str) -> List[str]:
    """Fallback parser extracting the next hop from ``ip route get``."""

    match = re.search(r"via\s+(\b(?:\d{1,3}\.){3}\d{1,3}\b)", output)
    if match:
        return [match.group(1)]
    return []


def count_prefixes_in_json(data: object) -> int:
    """Count occurrences of the ``prefix`` key recursively."""

    if isinstance(data, dict):
        count = 1 if "prefix" in data else 0
        for value in data.values():
            count += count_prefixes_in_json(value)
        return count
    if isinstance(data, list):
        return sum(count_prefixes_in_json(item) for item in data)
    return 0


def estimate_route_count_from_text(output: str) -> int:
    """Best-effort parser for FRR routing table text output."""

    count = 0
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Codes:") or stripped.startswith("Gateway of last resort"):
            continue
        if "/" in stripped and ("via" in stripped or stripped[0].isalpha()):
            count += 1
    return count


def count_control_packets_from_text(output: str) -> int:
    """Count protocol keywords as an approximation of control packets."""

    total = 0
    for keyword in CONTROL_KEYWORDS:
        total += len(re.findall(keyword, output, flags=re.IGNORECASE))
    return total


def count_control_packets_from_json(data: object) -> int:
    """Traverse JSON structures and sum control-packet counters."""

    total = 0
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (int, float)) and any(word in key.lower() for word in CONTROL_KEYWORDS):
                total += int(value)
            else:
                total += count_control_packets_from_json(value)
    elif isinstance(data, list):
        for item in data:
            total += count_control_packets_from_json(item)
    return total


def parse_frr_routing_table(router: str) -> tuple[int, str]:
    """Retrieve and count FRRouting table entries."""

    json_result = run_command(["docker", "exec", router, "vtysh", "-c", "show ip route json"])
    table_output = json_result.stdout.strip()
    num_routes = 0

    if json_result.returncode == 0 and table_output:
        try:
            table_json = json.loads(table_output)
        except json.JSONDecodeError:
            num_routes = estimate_route_count_from_text(table_output)
        else:
            num_routes = count_prefixes_in_json(table_json)
            table_output = json.dumps(table_json, indent=2, sort_keys=True)
    else:
        text_result = run_command(["docker", "exec", router, "vtysh", "-c", "show ip route"])
        table_output = text_result.stdout
        num_routes = estimate_route_count_from_text(table_output)

    save_raw("ospf_native", f"{router}_routes", table_output, "json" if table_output.startswith("{") else "txt")
    return num_routes, table_output


def parse_frr_control_packets(router: str) -> int:
    """Approximate FRR control traffic using ``show ip ospf statistics``."""

    stats_result = run_command(["docker", "exec", router, "vtysh", "-c", "show ip ospf statistics json"])
    output = stats_result.stdout.strip()
    if stats_result.returncode == 0 and output:
        try:
            stats_json = json.loads(output)
        except json.JSONDecodeError:
            pass
        else:
            save_raw("ospf_native", f"{router}_ospf_stats", json.dumps(stats_json, indent=2, sort_keys=True), "json")
            return count_control_packets_from_json(stats_json)

    # Fallback to text output
    stats_result = run_command(["docker", "exec", router, "vtysh", "-c", "show ip ospf statistics"])
    output = stats_result.stdout
    save_raw("ospf_native", f"{router}_ospf_stats", output)
    return count_control_packets_from_text(output)


def extract_json_from_logs(log_output: str) -> Optional[dict]:
    """Find the latest JSON blob embedded in container logs."""

    for line in reversed(log_output.splitlines()):
        start = line.find("{")
        end = line.rfind("}")
        if start == -1 or end == -1 or end <= start:
            continue
        snippet = line[start : end + 1]
        try:
            candidate = json.loads(snippet)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


def parse_ospf_gaming_snapshot(router: str) -> tuple[int, Optional[dict], str]:
    """Retrieve the latest OSPF-Gaming routing table snapshot and logs."""

    candidate_paths = (
        "/var/log/ospf_gaming/routes.json",
        "/opt/ospf-gaming/routes.json",
        "/opt/ospf-gaming/state/routes.json",
        "/tmp/ospf_gaming_routes.json",
    )

    for path in candidate_paths:
        result = run_command(["docker", "exec", router, "sh", "-lc", f"cat {shlex.quote(path)}"], timeout=15)
        content = result.stdout.strip()
        if result.returncode == 0 and content:
            try:
                snapshot = json.loads(content)
            except json.JSONDecodeError:
                continue
            save_raw("ospf_gaming", f"{router}_routes", json.dumps(snapshot, indent=2, sort_keys=True), "json")
            return count_prefixes_in_json(snapshot), snapshot, ""

    # Fallback to docker logs
    logs_result = run_command(["docker", "logs", router])
    log_output = logs_result.stdout
    save_raw("ospf_gaming", f"{router}_logs", log_output)
    snapshot = extract_json_from_logs(log_output)
    if snapshot:
        num_routes = count_prefixes_in_json(snapshot)
        save_raw("ospf_gaming", f"{router}_routes", json.dumps(snapshot, indent=2, sort_keys=True), "json")
        return num_routes, snapshot, log_output

    return 0, None, log_output


def parse_ospf_gaming_control_packets(router: str, log_output: Optional[str] = None) -> int:
    """Count protocol events from OSPF-Gaming logs."""

    if log_output is None:
        logs_result = run_command(["docker", "logs", router])
        log_output = logs_result.stdout
        if log_output:
            save_raw("ospf_gaming", f"{router}_logs", log_output)
    return count_control_packets_from_text(log_output or "")


def collect_routing_tables(router_names: Sequence[str] = ROUTERS) -> Dict[str, Dict[str, dict]]:
    """Collect routing table information for all routers."""

    results: Dict[str, Dict[str, dict]] = {protocol: {} for protocol in PROTOCOLS}

    for router in router_names:
        num_routes, table_output = parse_frr_routing_table(router)
        control_packets = parse_frr_control_packets(router)
        results["ospf_native"][router] = {
            "num_routes": num_routes,
            "control_packets": control_packets,
            "raw_table": table_output,
            "snapshot": None,
        }

        gaming_routes, gaming_snapshot, gaming_logs = parse_ospf_gaming_snapshot(router)
        gaming_control = parse_ospf_gaming_control_packets(router, gaming_logs)
        results["ospf_gaming"][router] = {
            "num_routes": gaming_routes,
            "control_packets": gaming_control,
            "raw_table": json.dumps(gaming_snapshot, indent=2, sort_keys=True) if gaming_snapshot else "",
            "snapshot": gaming_snapshot,
        }

    return results


@contextlib.contextmanager
def protocol_mode(protocol: str) -> Iterator[None]:
    """Optional hook to adjust the network before measuring a protocol.

    Users may export environment variables named ``<PROTOCOL>_PRE_COMMAND`` and
    ``<PROTOCOL>_POST_COMMAND`` with shell snippets executed before and after the
    measurements respectively (for example, to toggle zebra/ospfd services).
    """

    env_prefix = protocol.upper()
    pre_cmd = os.environ.get(f"{env_prefix}_PRE_COMMAND")
    post_cmd = os.environ.get(f"{env_prefix}_POST_COMMAND")

    if pre_cmd:
        _LOGGER.info("Executing pre-command for %s: %s", protocol, pre_cmd)
        run_command(["/bin/sh", "-c", pre_cmd], timeout=120)

    try:
        yield
    finally:
        if post_cmd:
            _LOGGER.info("Executing post-command for %s: %s", protocol, post_cmd)
            run_command(["/bin/sh", "-c", post_cmd], timeout=120)


def measure_ping(src: str, dst: str, count: int = 10, interval: float = 0.2) -> tuple[Optional[float], Optional[float], Optional[float], str]:
    """Run a ping test from ``src`` to ``dst`` via Docker."""

    cmd = [
        "docker",
        "exec",
        src,
        "ping",
        "-c",
        str(count),
        "-i",
        str(interval),
        dst,
    ]
    result = run_command(cmd, timeout=int(count * (interval + 1)) + 5)
    output = (result.stdout or "") + (result.stderr or "")
    latency, jitter, loss = parse_ping_output(output)
    return latency, jitter, loss, output


def measure_traceroute(src: str, dst: str) -> tuple[List[str], str]:
    """Discover the hop-by-hop path between two hosts."""

    traceroute_cmd = ["docker", "exec", src, "traceroute", "-n", "-m", "10", dst]
    result = run_command(traceroute_cmd, timeout=60)
    output = result.stdout or ""
    if result.returncode == 0 and output:
        path = parse_traceroute_output(output)
        if path:
            return path, output

    route_cmd = ["docker", "exec", src, "ip", "route", "get", dst]
    route_result = run_command(route_cmd, timeout=15)
    output += "\n" + (route_result.stdout or "")
    path = parse_ip_route_get(route_result.stdout)
    return path, output


def measure_iperf(src: str, dst: str, duration: int = 10) -> tuple[Optional[float], str]:
    """Run an iperf3 throughput test."""

    server_cmd = ["docker", "exec", dst, "iperf3", "-s", "-1"]
    try:
        server_proc = subprocess.Popen(
            server_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        _LOGGER.warning("iperf3 not available in %s", dst)
        return None, "iperf3 not available"
    except Exception as exc:  
        _LOGGER.error("Failed to spawn iperf3 server on %s: %s", dst, exc)
        return None, str(exc)

    time.sleep(1.5)

    client_cmd = [
        "docker",
        "exec",
        src,
        "iperf3",
        "-c",
        dst,
        "-t",
        str(duration),
        "-J",
    ]

    client_result = run_command(client_cmd, timeout=duration + 30)
    client_output = (client_result.stdout or "") + (client_result.stderr or "")

    try:
        server_stdout, server_stderr = server_proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        server_proc.kill()
        server_stdout, server_stderr = server_proc.communicate()

    combined_output = client_output + (server_stdout or "") + (server_stderr or "")

    throughput = parse_iperf_json(client_result.stdout or "")
    if throughput is None:
        throughput = parse_iperf_text(combined_output)

    return throughput, combined_output


def collect_metrics(host_pair: Sequence[str] = HOSTS) -> Dict[str, dict]:
    """Collect end-to-end metrics from the host containers."""

    if len(host_pair) != 2:
        raise ValueError("host_pair must contain exactly two host identifiers")

    src, dst = host_pair
    metrics: Dict[str, dict] = {}

    for protocol in PROTOCOLS:
        _LOGGER.info("Collecting end-to-end metrics for %s", protocol)
        with protocol_mode(protocol):
            latency, jitter, loss, ping_output = measure_ping(src, dst)
            path, traceroute_output = measure_traceroute(src, dst)
            throughput, iperf_output = measure_iperf(src, dst)

        metrics[protocol] = {
            "latency": latency,
            "jitter": jitter,
            "packet_loss": loss,
            "throughput": throughput,
            "path": path,
        }

        save_raw(protocol, "ping", ping_output)
        save_raw(protocol, "path", traceroute_output)
        save_raw(protocol, "iperf", iperf_output)

    return metrics


def derive_path_from_routing(router_data: Dict[str, dict]) -> List[str]:
    """Attempt to infer the preferred path from routing snapshots."""

    for info in router_data.values():
        snapshot = info.get("snapshot")
        if isinstance(snapshot, dict):
            for key in ("path", "best_path", "selected_path"):
                value = snapshot.get(key)
                if isinstance(value, list):
                    return [str(item) for item in value]
                if isinstance(value, str):
                    return [hop.strip() for hop in value.split("->") if hop.strip()]
    return []


def compare_protocols(routing_data: Dict[str, Dict[str, dict]], metrics: Dict[str, dict]) -> Dict[str, dict]:
    """Aggregate the collected data by protocol."""

    comparison: Dict[str, dict] = {}
    for protocol in PROTOCOLS:
        routers = routing_data.get(protocol, {})
        total_routes = sum(info.get("num_routes", 0) or 0 for info in routers.values())
        total_control = sum(info.get("control_packets", 0) or 0 for info in routers.values())
        metric_entry = metrics.get(protocol, {})
        path = metric_entry.get("path") or derive_path_from_routing(routers)

        comparison[protocol] = {
            "routing_table_size": total_routes,
            "control_overhead": total_control,
            "latency_ms": metric_entry.get("latency"),
            "jitter_ms": metric_entry.get("jitter"),
            "packet_loss_pct": metric_entry.get("packet_loss"),
            "throughput_mbps": metric_entry.get("throughput"),
            "path": path,
        }

    return comparison


def write_csv(path: Path, rows: List[dict], fieldnames: Sequence[str]) -> None:
    """Persist a list of dictionaries as CSV."""

    with path.open("w", encoding="utf-8", newline="") as handler:
        writer = csv.DictWriter(handler, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def store_results(
    routing_data: Dict[str, Dict[str, dict]],
    metrics: Dict[str, dict],
    comparison: Dict[str, dict],
) -> None:
    """Save intermediate CSV artefacts."""

    routing_rows: List[dict] = []
    for protocol, routers in routing_data.items():
        for router, info in routers.items():
            routing_rows.append(
                {
                    "protocol": protocol,
                    "router": router,
                    "num_routes": info.get("num_routes", 0),
                    "control_packets": info.get("control_packets", 0),
                }
            )

    write_csv(RESULTS_DIR / "routing_tables.csv", routing_rows, ("protocol", "router", "num_routes", "control_packets"))

    metric_rows: List[dict] = []
    for protocol, values in metrics.items():
        metric_rows.append(
            {
                "protocol": protocol,
                "latency_ms": values.get("latency"),
                "jitter_ms": values.get("jitter"),
                "packet_loss_pct": values.get("packet_loss"),
                "throughput_mbps": values.get("throughput"),
                "path": " -> ".join(values.get("path") or []),
            }
        )

    write_csv(
        RESULTS_DIR / "end_to_end_metrics.csv",
        metric_rows,
        ("protocol", "latency_ms", "jitter_ms", "packet_loss_pct", "throughput_mbps", "path"),
    )

    comparison_rows: List[dict] = []
    for protocol, values in comparison.items():
        row = {"protocol": protocol}
        row.update(values)
        row["path"] = " -> ".join(values.get("path") or [])
        comparison_rows.append(row)

    write_csv(
        RESULTS_DIR / "comparison_summary.csv",
        comparison_rows,
        (
            "protocol",
            "routing_table_size",
            "control_overhead",
            "latency_ms",
            "jitter_ms",
            "packet_loss_pct",
            "throughput_mbps",
            "path",
        ),
    )


def plot_results(comparison: Dict[str, dict]) -> None:
    """Generate comparison plots for the collected metrics."""

    if plt is None:  
        _LOGGER.warning("matplotlib is not available; skipping plot generation")
        return

    metrics_to_plot = [
        ("latency_ms", "Latency (ms)", "latency_vs_protocol.png"),
        ("jitter_ms", "Jitter (ms)", "jitter_vs_protocol.png"),
        ("packet_loss_pct", "Packet Loss (%)", "loss_vs_protocol.png"),
        ("routing_table_size", "Routing Table Entries", "routing_table_size_vs_protocol.png"),
        ("control_overhead", "Control Packets (approx.)", "control_overhead_vs_protocol.png"),
    ]

    labels = list(comparison.keys())

    for metric_key, ylabel, filename in metrics_to_plot:
        values: List[float] = []
        for protocol in labels:
            value = comparison[protocol].get(metric_key)
            if value is None:
                values.append(float("nan"))
            else:
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    values.append(float("nan"))

        plt.figure(figsize=(6, 4))
        bars = plt.bar(labels, values, color=["#1f77b4", "#ff7f0e"])
        plt.ylabel(ylabel)
        plt.title(f"{ylabel} by Protocol")
        plt.grid(axis="y", linestyle="--", alpha=0.3)

        for bar, value in zip(bars, values):
            if math.isnan(value):
                continue
            plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.2f}", ha="center", va="bottom")

        plt.tight_layout()
        output_path = RESULTS_DIR / filename
        plt.savefig(output_path, dpi=200)
        plt.close()
        _LOGGER.info("Saved plot %s", output_path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ensure_results_dirs()

    _LOGGER.info("Starting routing data collection")
    routing_tables = collect_routing_tables()

    _LOGGER.info("Collecting end-to-end metrics")
    metrics = collect_metrics()

    _LOGGER.info("Computing comparison summary")
    comparison = compare_protocols(routing_tables, metrics)

    _LOGGER.info("Persisting results")
    store_results(routing_tables, metrics, comparison)

    _LOGGER.info("Generating plots")
    plot_results(comparison)

    _LOGGER.info("Analysis complete. Results stored in %s", RESULTS_DIR)


if __name__ == "__main__":
    main()

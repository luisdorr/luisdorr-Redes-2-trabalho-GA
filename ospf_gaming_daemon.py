"""Main execution entrypoint for the OSPF-Gaming routing daemon."""

from __future__ import annotations

import argparse
import json
import logging
import math
import socket
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from algorithm import calculate_shortest_paths
from metrics import get_static_bandwidth, measure_link_quality
from route_manager import add_route, delete_route

DEFAULT_CONFIG_PATH = Path("config/config.json")
DEFAULT_PORT = 55000
HELLO_INTERVAL = 5
METRIC_INTERVAL = 30

_LOGGER = logging.getLogger(__name__)


class OSPFGamingDaemon:
    """Encapsulates the state machine of the OSPF-Gaming protocol."""

    def __init__(self, config_path: Path) -> None:
        self.config = self._load_config(config_path)
        self.router_id: str = self.config["router_id"]
        self.listen_ip: str = self.config.get("listen_ip", "0.0.0.0")
        self.listen_port: int = self.config.get("listen_port", DEFAULT_PORT)
        self.hello_interval: int = self.config.get("hello_interval", HELLO_INTERVAL)
        self.metric_interval: int = self.config.get("metric_interval", METRIC_INTERVAL)

        neighbor_entries = self.config.get("neighbors", [])
        self.neighbor_settings: Dict[str, Dict[str, Any]] = {
            entry["id"]: entry for entry in neighbor_entries
        }

        self.neighbors: Dict[str, Dict[str, Any]] = {}
        for neighbor_id, settings in self.neighbor_settings.items():
            bandwidth = settings.get("bandwidth") or get_static_bandwidth(
                self.router_id, neighbor_id
            )
            self.neighbors[neighbor_id] = {
                "ip": settings["ip"],
                "port": settings.get("port", self.listen_port),
                "interface": settings.get("interface"),
                "last_hello": 0.0,
                "metrics": {
                    "latency": float("inf"),
                    "jitter": float("inf"),
                    "loss": 100.0,
                    "bandwidth": bandwidth,
                },
            }

        # Topology and routing
        self.topology_graph: Dict[str, Dict[str, float]] = {self.router_id: {}}
        self.routing_table: Dict[str, str] = {}
        self.installed_routes: Dict[str, str] = {}
        self.lsa_versions: Dict[str, int] = {}  # track seqnum per origin

        self._state_lock = threading.Lock()
        self._running = threading.Event()
        self._running.set()

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.listen_ip, self.listen_port))
        self._socket.settimeout(1.0)

        self._threads: list[threading.Thread] = []
        self._seqnum = 0  # local LSA sequence number

    # ------------------------------------------------------------------
    # Lifecycle management
    # ------------------------------------------------------------------
    def start(self) -> None:
        _LOGGER.info("Starting OSPF-Gaming daemon for router %s", self.router_id)

        self._threads = [
            threading.Thread(target=self._hello_loop, name="hello", daemon=True),
            threading.Thread(target=self._listen_loop, name="listener", daemon=True),
            threading.Thread(target=self._metric_loop, name="metrics", daemon=True),
        ]

        for thread in self._threads:
            thread.start()

        try:
            while self._running.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            _LOGGER.info("Keyboard interrupt received, stopping daemon")
            self.stop()

    def stop(self) -> None:
        self._running.clear()
        self._socket.close()
        for thread in self._threads:
            thread.join(timeout=2)
        self._flush_routes()

    # ------------------------------------------------------------------
    # Core protocol loops
    # ------------------------------------------------------------------
    def _hello_loop(self) -> None:
        while self._running.is_set():
            for neighbor_id in list(self.neighbors.keys()):
                message = {
                    "type": "hello",
                    "router_id": self.router_id,
                    "timestamp": time.time(),
                }
                self._send_message(neighbor_id, message)
            time.sleep(self.hello_interval)

    def _listen_loop(self) -> None:
        while self._running.is_set():
            try:
                payload, (source_ip, _) = self._socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                message = json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError:
                _LOGGER.warning("Received malformed packet from %s", source_ip)
                continue

            packet_type = message.get("type")
            if packet_type == "hello":
                self._process_hello(message, source_ip)
            elif packet_type == "lsa":
                self._process_lsa(message)
            else:
                _LOGGER.debug("Ignoring unsupported packet type: %s", packet_type)

    def _metric_loop(self) -> None:
        while self._running.is_set():
            self._update_link_metrics()
            self._broadcast_lsa()
            self._recalculate_routes()
            time.sleep(self.metric_interval)

    # ------------------------------------------------------------------
    # Packet handlers
    # ------------------------------------------------------------------
    def _process_hello(self, message: Dict[str, Any], source_ip: str) -> None:
        neighbor_id = message.get("router_id")
        if neighbor_id == self.router_id:
            return
        if neighbor_id not in self.neighbors:
            return

        with self._state_lock:
            self.neighbors[neighbor_id]["last_hello"] = time.time()
            self.neighbors[neighbor_id].setdefault("ip", source_ip)

    def _process_lsa(self, message: Dict[str, Any]) -> None:
        origin = message.get("router_id")
        links = message.get("neighbors", {})
        seqnum = int(message.get("seqnum", 0))
        if not origin:
            return

        with self._state_lock:
            current_seq = self.lsa_versions.get(origin, -1)
            if seqnum <= current_seq:
                _LOGGER.debug(
                    "Discarding old LSA from %s (seqnum=%s, current=%s)",
                    origin,
                    seqnum,
                    current_seq,
                )
                return

            self.lsa_versions[origin] = seqnum
            if origin not in self.topology_graph:
                self.topology_graph[origin] = {}
            self.topology_graph[origin].update({k: float(v) for k, v in links.items()})
            _LOGGER.debug("Updated topology with LSA from %s seq=%s", origin, seqnum)

        # re-flood to neighbors except the origin
        for neighbor_id in list(self.neighbors.keys()):
            if neighbor_id != origin:
                self._send_message(neighbor_id, message)

    # ------------------------------------------------------------------
    # Metrics and topology management
    # ------------------------------------------------------------------
    def _update_link_metrics(self) -> None:
        for neighbor_id, neighbor in self.neighbors.items():
            ip_address = neighbor.get("ip")
            if not ip_address:
                continue

            latency, jitter, loss = measure_link_quality(ip_address)
            bandwidth = neighbor["metrics"].get("bandwidth")
            if bandwidth is None:
                bandwidth = get_static_bandwidth(self.router_id, neighbor_id)
                neighbor["metrics"]["bandwidth"] = bandwidth

            cost = self._calculate_cost(latency, jitter, loss, bandwidth)

            with self._state_lock:
                neighbor["metrics"].update(
                    {
                        "latency": latency,
                        "jitter": jitter,
                        "loss": loss,
                        "cost": cost,
                    }
                )
                self.topology_graph.setdefault(self.router_id, {})[neighbor_id] = cost
                self.topology_graph.setdefault(neighbor_id, {})[self.router_id] = cost

            _LOGGER.debug(
                "Metrics for %s -> latency: %.2f ms, jitter: %.2f ms, loss: %.2f%%, cost: %.2f",
                neighbor_id,
                latency,
                jitter,
                loss,
                cost,
            )

    def _calculate_cost(
        self,
        latency: float,
        jitter: float,
        loss: float,
        bandwidth: Optional[int],
    ) -> float:
        if not math.isfinite(latency) or not math.isfinite(jitter) or loss >= 100.0:
            return float("inf")

        penalty = loss * 10.0 + jitter
        normalized_latency = latency
        if bandwidth and bandwidth > 0:
            bandwidth_penalty = 1000.0 / bandwidth
        else:
            bandwidth_penalty = 1000.0

        return normalized_latency + penalty + bandwidth_penalty

    def _broadcast_lsa(self) -> None:
        with self._state_lock:
            local_view = deepcopy(self.topology_graph.get(self.router_id, {}))
            self._seqnum += 1
            message = {
                "type": "lsa",
                "router_id": self.router_id,
                "neighbors": local_view,
                "seqnum": self._seqnum,
                "timestamp": time.time(),
            }

        for neighbor_id in list(self.neighbors.keys()):
            self._send_message(neighbor_id, message)

    def _recalculate_routes(self) -> None:
        with self._state_lock:
            topology_snapshot = deepcopy(self.topology_graph)

        new_routing_table = calculate_shortest_paths(topology_snapshot, self.router_id)
        _LOGGER.debug("Routing table recomputed: %s", new_routing_table)
        self._synchronise_kernel_routes(new_routing_table)

        with self._state_lock:
            self.routing_table = new_routing_table

    # ------------------------------------------------------------------
    # Routing table management
    # ------------------------------------------------------------------
    def _synchronise_kernel_routes(self, new_routes: Dict[str, str]) -> None:
        for destination, prefix, interface in self._iter_route_targets():
            next_hop_id = new_routes.get(destination)
            if not next_hop_id:
                self._remove_installed_route(prefix)
                continue

            next_hop_ip = self._resolve_next_hop_ip(next_hop_id)
            if not next_hop_ip:
                continue

            current_next_hop = self.installed_routes.get(prefix)
            if current_next_hop == next_hop_ip:
                continue

            try:
                if current_next_hop:
                    delete_route(prefix)
                add_route(prefix, next_hop_ip, interface)
                self.installed_routes[prefix] = next_hop_ip
            except (subprocess.CalledProcessError, OSError):
                _LOGGER.exception("Failed to install route to %s via %s", prefix, next_hop_ip)

        active_prefixes = {prefix for _, prefix, _ in self._iter_route_targets()}
        for prefix in list(self.installed_routes.keys()):
            if prefix not in active_prefixes:
                self._remove_installed_route(prefix)

    def _iter_route_targets(self) -> Iterable[tuple[str, str, Optional[str]]]:
        route_mappings = self.config.get("route_mappings", {})
        for destination, mapping in route_mappings.items():
            if isinstance(mapping, str):
                yield destination, mapping, None
            elif isinstance(mapping, dict):
                yield destination, mapping["prefix"], mapping.get("interface")

    def _remove_installed_route(self, prefix: str) -> None:
        if prefix not in self.installed_routes:
            return
        try:
            delete_route(prefix)
        except (subprocess.CalledProcessError, OSError):
            _LOGGER.exception("Failed to remove stale route for %s", prefix)
        else:
            self.installed_routes.pop(prefix, None)

    def _resolve_next_hop_ip(self, next_hop_id: str) -> Optional[str]:
        neighbor = self.neighbors.get(next_hop_id)
        if neighbor:
            return neighbor.get("ip")
        return None

    def _flush_routes(self) -> None:
        for prefix in list(self.installed_routes.keys()):
            self._remove_installed_route(prefix)

    # ------------------------------------------------------------------
    # Networking helpers
    # ------------------------------------------------------------------
    def _send_message(self, neighbor_id: str, message: Dict[str, Any]) -> None:
        neighbor = self.neighbors.get(neighbor_id)
        if not neighbor:
            return

        payload = json.dumps(message).encode("utf-8")
        try:
            self._socket.sendto(payload, (neighbor["ip"], neighbor["port"]))
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _load_config(config_path: Path) -> Dict[str, Any]:
        with config_path.open("r", encoding="utf-8") as handler:
            return json.load(handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OSPF-Gaming QoS-aware routing daemon")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to the JSON configuration file")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    daemon = OSPFGamingDaemon(args.config)
    daemon.start()


if __name__ == "__main__":
    main()

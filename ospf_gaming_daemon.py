"""QoS-aware link-state routing daemon for the OSPF-Gaming assignment."""

from __future__ import annotations

import argparse
import json
import logging
import math
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set

from algorithm import PathInfo, calculate_shortest_paths
from metrics import (
    MetricWeights,
    NormalizationBounds,
    QoSMetrics,
    compute_qos_cost,
    get_reference_bandwidth,
    measure_link_quality,
)
from route_manager import add_route, delete_route

DEFAULT_CONFIG_PATH = Path("config/config.json")
DEFAULT_PORT = 55000
HELLO_INTERVAL = 5
DEAD_INTERVAL = 20
METRIC_INTERVAL = 30
LSA_TTL_HOPS = 8
LSA_MAX_AGE = 120

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class NeighborConfig:
    router_id: str
    ip: str
    port: int
    interface: Optional[str]
    bandwidth: Optional[float]


@dataclass(slots=True)
class NeighborState:
    config: NeighborConfig
    metrics: QoSMetrics
    last_hello: float = 0.0
    is_up: bool = False


@dataclass(slots=True)
class LinkSnapshot:
    cost: float
    metrics: QoSMetrics
    updated_at: float

    def to_payload(self) -> Dict[str, Any]:
        return {
            "cost": self.cost,
            "latency_ms": self.metrics.latency_ms,
            "jitter_ms": self.metrics.jitter_ms,
            "loss_percent": self.metrics.loss_percent,
            "bandwidth_mbps": self.metrics.bandwidth_mbps,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "LinkSnapshot":
        metrics = QoSMetrics(
            latency_ms=float(payload.get("latency_ms", math.inf)),
            jitter_ms=float(payload.get("jitter_ms", math.inf)),
            loss_percent=float(payload.get("loss_percent", 100.0)),
            bandwidth_mbps=payload.get("bandwidth_mbps"),
        )
        return cls(cost=float(payload.get("cost", 100.0)), metrics=metrics, updated_at=time.time())


@dataclass(slots=True)
class LSADBEntry:
    seq: int
    links: Dict[str, LinkSnapshot]
    prefixes: Set[str]
    received_at: float


class OSPFGamingDaemon:
    """Link-state control plane with Hello, QoS LSAs, and kernel FIB synchronisation."""

    def __init__(self, config_path: Path) -> None:
        self.config = self._load_config(config_path)
        self.router_id: str = self.config["router_id"]
        self.listen_ip: str = self.config.get("listen_ip", "0.0.0.0")
        self.listen_port: int = self.config.get("listen_port", DEFAULT_PORT)
        self.hello_interval: int = self.config.get("hello_interval", HELLO_INTERVAL)
        self.dead_interval: int = self.config.get("dead_interval", DEAD_INTERVAL)
        self.metric_interval: int = self.config.get("metric_interval", METRIC_INTERVAL)
        self.ping_count: int = self.config.get("ping_count", 10)
        self.ping_interval: float = self.config.get("ping_interval", 0.2)

        weights_cfg = self.config.get("weights_percent", {})
        self.metric_weights = MetricWeights(
            latency=float(weights_cfg.get("latency", 25.0)),
            jitter=float(weights_cfg.get("jitter", 35.0)),
            loss=float(weights_cfg.get("loss", 30.0)),
            bandwidth=float(weights_cfg.get("bandwidth", 10.0)),
        )

        norm_cfg = self.config.get("normalization", {})
        self.normalization = NormalizationBounds(
            latency_ms=float(norm_cfg.get("latency_max_ms", 100.0)),
            jitter_ms=float(norm_cfg.get("jitter_max_ms", 20.0)),
            loss_percent=float(norm_cfg.get("loss_max_percent", 100.0)),
            bandwidth_mbps=float(norm_cfg.get("bandwidth_ref_mbps", 1000.0)),
        )

        self.route_mappings: Dict[str, Set[str]] = {
            router: set(prefixes)
            for router, prefixes in self.config.get("route_mappings", {}).items()
        }

        self.neighbor_configs: Dict[str, NeighborConfig] = {}
        for entry in self.config.get("neighbors", []):
            neighbor_id = entry["id"]
            cfg = NeighborConfig(
                router_id=neighbor_id,
                ip=entry["ip"],
                port=int(entry.get("port", self.listen_port)),
                interface=entry.get("interface"),
                bandwidth=entry.get("bandwidth") or get_reference_bandwidth(self.router_id, neighbor_id),
            )
            self.neighbor_configs[neighbor_id] = cfg

        self.neighbors: Dict[str, NeighborState] = {
            rid: NeighborState(
                config=cfg,
                metrics=QoSMetrics(
                    latency_ms=math.inf,
                    jitter_ms=math.inf,
                    loss_percent=100.0,
                    bandwidth_mbps=cfg.bandwidth,
                ),
            )
            for rid, cfg in self.neighbor_configs.items()
        }

        self.local_prefixes: Set[str] = set(self.config.get("local_prefixes", []))
        for cfg in self.neighbor_configs.values():
            prefix = self._infer_link_prefix(cfg.ip)
            if prefix:
                self.local_prefixes.add(prefix)

        self._local_links: Dict[str, LinkSnapshot] = {}
        self._lsa_seq = 1
        self.lsdb: Dict[str, LSADBEntry] = {
            self.router_id: LSADBEntry(
                seq=self._lsa_seq,
                links={},
                prefixes=set(self.local_prefixes),
                received_at=time.time(),
            )
        }

        self.routing_table: Dict[str, PathInfo] = {}
        self.installed_routes: Dict[str, str] = {}

        self._state_lock = threading.Lock()
        self._running = threading.Event()
        self._running.set()

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.listen_ip, self.listen_port))
        self._socket.settimeout(1.0)

        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        _LOGGER.info("Starting OSPF-Gaming daemon for %s", self.router_id)
        self._threads = [
            threading.Thread(target=self._receiver_loop, name=f"{self.router_id}-rx", daemon=True),
            threading.Thread(target=self._hello_loop, name=f"{self.router_id}-hello", daemon=True),
            threading.Thread(target=self._metric_loop, name=f"{self.router_id}-metrics", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

        self._broadcast_lsa(self.router_id, self.lsdb[self.router_id], ttl=LSA_TTL_HOPS, exclude=None)

        try:
            while self._running.is_set():
                self._sleep(1.0)
        except KeyboardInterrupt:
            _LOGGER.info("Shutdown requested by user")
        finally:
            self.stop()
            for thread in self._threads:
                thread.join()

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        try:
            self._socket.close()
        except OSError:
            pass
        self._flush_routes()

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while self._running.is_set() and time.time() < end:
            remaining = end - time.time()
            time.sleep(min(remaining, 1.0))

    def _hello_loop(self) -> None:
        payload_template = {"type": "hello", "router_id": self.router_id}
        while self._running.is_set():
            now = time.time()
            payload = dict(payload_template)
            payload["timestamp"] = now
            for neighbor_id in self.neighbor_configs:
                self._send_message(neighbor_id, payload)
            self._check_dead_neighbors(now)
            self._sleep(self.hello_interval)

    def _check_dead_neighbors(self, now: float) -> None:
        expired: list[str] = []
        with self._state_lock:
            for neighbor_id, state in self.neighbors.items():
                if not state.is_up:
                    continue
                if now - state.last_hello > self.dead_interval:
                    state.is_up = False
                    state.last_hello = 0.0
                    state.metrics = QoSMetrics(
                        latency_ms=math.inf,
                        jitter_ms=math.inf,
                        loss_percent=100.0,
                        bandwidth_mbps=state.config.bandwidth,
                    )
                    expired.append(neighbor_id)
        for neighbor_id in expired:
            _LOGGER.warning("%s lost adjacency to %s", self.router_id, neighbor_id)
            self._drop_local_link(neighbor_id)

    def _drop_local_link(self, neighbor_id: str) -> None:
        removed = False
        with self._state_lock:
            if neighbor_id in self._local_links:
                self._local_links.pop(neighbor_id, None)
                removed = True
        if removed:
            entry = self._update_local_lsa()
            self._broadcast_lsa(self.router_id, entry, ttl=LSA_TTL_HOPS, exclude=None)
            self._recompute_routes()

    def _metric_loop(self) -> None:
        while self._running.is_set():
            changed = False
            for neighbor_id, state in self.neighbors.items():
                metrics = measure_link_quality(
                    state.config.ip,
                    count=self.ping_count,
                    interval=self.ping_interval,
                    bandwidth_hint=state.config.bandwidth,
                )
                with self._state_lock:
                    state.metrics = metrics
                    if state.is_up:
                        changed |= self._update_local_link(neighbor_id, metrics)
            if changed:
                entry = self._update_local_lsa()
                self._broadcast_lsa(self.router_id, entry, ttl=LSA_TTL_HOPS, exclude=None)
                self._recompute_routes()
            self._purge_stale_lsas()
            self._sleep(self.metric_interval)

    def _update_local_link(self, neighbor_id: str, metrics: QoSMetrics) -> bool:
        cost = compute_qos_cost(metrics, self.metric_weights, self.normalization)
        snapshot = LinkSnapshot(cost=cost, metrics=metrics, updated_at=time.time())
        current = self._local_links.get(neighbor_id)
        if current and self._link_equivalent(current, snapshot):
            self._local_links[neighbor_id] = snapshot
            return False
        self._local_links[neighbor_id] = snapshot
        return True

    @staticmethod
    def _link_equivalent(current: LinkSnapshot, candidate: LinkSnapshot) -> bool:
        if not math.isfinite(current.cost) and not math.isfinite(candidate.cost):
            return True
        if abs(current.cost - candidate.cost) > 0.5:
            return False
        if abs(current.metrics.latency_ms - candidate.metrics.latency_ms) > 1.0:
            return False
        if abs(current.metrics.jitter_ms - candidate.metrics.jitter_ms) > 1.0:
            return False
        if abs(current.metrics.loss_percent - candidate.metrics.loss_percent) > 1.0:
            return False
        return True

    def _receiver_loop(self) -> None:
        while self._running.is_set():
            try:
                payload, addr = self._socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            message_type = message.get("type")
            if message_type == "hello":
                self._handle_hello(message)
            elif message_type == "lsa":
                self._handle_lsa(message, addr)

    def _handle_hello(self, message: Dict[str, Any]) -> None:
        neighbor_id = message.get("router_id")
        if neighbor_id not in self.neighbors:
            return
        now = time.time()
        with self._state_lock:
            state = self.neighbors[neighbor_id]
            state.last_hello = now
            if not state.is_up:
                state.is_up = True
                _LOGGER.info("%s established adjacency with %s", self.router_id, neighbor_id)

    def _handle_lsa(self, message: Dict[str, Any], addr: tuple[str, int]) -> None:
        origin = message.get("origin")
        seq = message.get("seq")
        ttl = int(message.get("ttl", 0))
        if origin is None or seq is None or ttl <= 0:
            return
        links_payload = message.get("links", {})
        prefixes = set(message.get("prefixes", []))
        entry = LSADBEntry(
            seq=int(seq),
            links={neighbor: LinkSnapshot.from_payload(payload) for neighbor, payload in links_payload.items()},
            prefixes=prefixes,
            received_at=time.time(),
        )
        with self._state_lock:
            current = self.lsdb.get(origin)
            if current and current.seq >= entry.seq:
                return
            self.lsdb[origin] = entry
        self._recompute_routes()
        forwarder = self._identify_neighbor(addr[0])
        self._broadcast_lsa(origin, entry, ttl=ttl - 1, exclude=forwarder)

    def _identify_neighbor(self, ip_address: str) -> Optional[str]:
        for neighbor_id, cfg in self.neighbor_configs.items():
            if cfg.ip == ip_address:
                return neighbor_id
        return None

    def _broadcast_lsa(self, origin: str, entry: LSADBEntry, *, ttl: int, exclude: Optional[str]) -> None:
        if ttl <= 0:
            return
        payload = {
            "type": "lsa",
            "origin": origin,
            "seq": entry.seq,
            "ttl": ttl,
            "prefixes": sorted(entry.prefixes),
            "links": {neighbor: snapshot.to_payload() for neighbor, snapshot in entry.links.items()},
        }
        for neighbor_id in self.neighbor_configs:
            if neighbor_id == exclude:
                continue
            self._send_message(neighbor_id, payload)

    def _update_local_lsa(self) -> LSADBEntry:
        with self._state_lock:
            self._lsa_seq += 1
            entry = LSADBEntry(
                seq=self._lsa_seq,
                links={neighbor: snapshot for neighbor, snapshot in self._local_links.items()},
                prefixes=set(self.local_prefixes),
                received_at=time.time(),
            )
            self.lsdb[self.router_id] = entry
        return entry

    def _purge_stale_lsas(self) -> None:
        cutoff = time.time() - LSA_MAX_AGE
        removed: list[str] = []
        with self._state_lock:
            for origin, entry in list(self.lsdb.items()):
                if origin == self.router_id:
                    continue
                if entry.received_at < cutoff:
                    self.lsdb.pop(origin, None)
                    removed.append(origin)
        if removed:
            _LOGGER.info("%s removed stale LSAs: %s", self.router_id, ", ".join(removed))
            self._recompute_routes()

    def _recompute_routes(self) -> None:
        with self._state_lock:
            lsdb_snapshot = dict(self.lsdb)
            local_prefixes = set(self.local_prefixes)
        graph = self._build_graph(lsdb_snapshot)
        routes = calculate_shortest_paths(graph, self.router_id)
        self._sync_kernel_routes(routes, lsdb_snapshot, local_prefixes)
        with self._state_lock:
            self.routing_table = routes

    @staticmethod
    def _build_graph(lsdb_snapshot: Dict[str, LSADBEntry]) -> Dict[str, Dict[str, float]]:
        graph: Dict[str, Dict[str, float]] = {}
        for origin, entry in lsdb_snapshot.items():
            graph[origin] = {
                neighbor: snapshot.cost
                for neighbor, snapshot in entry.links.items()
                if math.isfinite(snapshot.cost)
            }
        return graph

    def _sync_kernel_routes(
        self,
        routes: Dict[str, PathInfo],
        lsdb_snapshot: Dict[str, LSADBEntry],
        local_prefixes: Set[str],
    ) -> None:
        desired: Dict[str, tuple[str, Optional[str]]] = {}
        for destination, path in routes.items():
            next_hop_id = path.next_hop
            next_hop_ip = self._resolve_next_hop_ip(next_hop_id)
            if not next_hop_ip:
                continue
            interface = None
            cfg = self.neighbor_configs.get(next_hop_id)
            if cfg:
                interface = cfg.interface
            for prefix in self._collect_prefixes(destination, lsdb_snapshot):
                if prefix in local_prefixes:
                    continue
                desired[prefix] = (next_hop_ip, interface)
        for prefix, (next_hop_ip, interface) in desired.items():
            current = self.installed_routes.get(prefix)
            if current == next_hop_ip:
                continue
            try:
                if current:
                    delete_route(prefix)
                add_route(prefix, next_hop_ip, interface=interface)
                self.installed_routes[prefix] = next_hop_ip
            except Exception:
                _LOGGER.exception("Failed to install route %s via %s", prefix, next_hop_ip)
        for prefix in list(self.installed_routes.keys()):
            if prefix not in desired:
                self._remove_installed_route(prefix)

    def _collect_prefixes(self, router_id: str, lsdb_snapshot: Dict[str, LSADBEntry]) -> Set[str]:
        prefixes = set(self.route_mappings.get(router_id, set()))
        entry = lsdb_snapshot.get(router_id)
        if entry:
            prefixes.update(entry.prefixes)
        if not prefixes and router_id in self.neighbor_configs:
            inferred = self._infer_link_prefix(self.neighbor_configs[router_id].ip)
            if inferred:
                prefixes.add(inferred)
        return prefixes

    def _remove_installed_route(self, prefix: str) -> None:
        if prefix not in self.installed_routes:
            return
        try:
            delete_route(prefix)
        except Exception:
            _LOGGER.exception("Failed to withdraw route %s", prefix)
        else:
            self.installed_routes.pop(prefix, None)

    def _send_message(self, neighbor_id: str, message: Dict[str, Any]) -> None:
        cfg = self.neighbor_configs.get(neighbor_id)
        if not cfg:
            return
        try:
            payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
            self._socket.sendto(payload, (cfg.ip, cfg.port))
        except OSError:
            _LOGGER.debug("Failed to send message to %s", neighbor_id, exc_info=True)

    def _resolve_next_hop_ip(self, neighbor_id: str) -> Optional[str]:
        cfg = self.neighbor_configs.get(neighbor_id)
        if cfg:
            return cfg.ip
        return None

    @staticmethod
    def _infer_link_prefix(ip_address: str) -> Optional[str]:
        parts = ip_address.split(".")
        if len(parts) != 4:
            return None
        return ".".join(parts[:3]) + ".0/24"

    def _flush_routes(self) -> None:
        for prefix in list(self.installed_routes.keys()):
            self._remove_installed_route(prefix)

    @staticmethod
    def _load_config(config_path: Path) -> Dict[str, Any]:
        with config_path.open("r", encoding="utf-8") as handler:
            return json.load(handler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OSPF-Gaming QoS-aware routing daemon")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to JSON config file")
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

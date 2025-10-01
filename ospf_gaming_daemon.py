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
from typing import Any, Dict, Optional, List, Set

from algorithm import calculate_shortest_paths
from metrics import get_static_bandwidth, measure_link_quality
from route_manager import add_route, delete_route

DEFAULT_CONFIG_PATH = Path("config/config.json")
DEFAULT_PORT = 55000
HELLO_INTERVAL = 5
METRIC_INTERVAL = 30
DEAD_INTERVAL = 20  # ~4x hello, como no OSPF real
LSA_TTL_HOPS = 8  # TTL simples para evitar loops de LSA

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
        
        # Configurable ping parameters
        self.ping_count: int = self.config.get("ping_count", 10)
        self.ping_interval: float = self.config.get("ping_interval", 0.2)
        
       # Configurable cost weights (prioritizing jitter)
        weights_percent = self.config.get("weights_percent", {})
        self.weight_latency: float = weights_percent.get("latency", 25.0)
        self.weight_jitter: float = weights_percent.get("jitter", 35.0)
        self.weight_loss: float = weights_percent.get("loss", 30.0)
        self.weight_bandwidth: float = weights_percent.get("bandwidth", 10.0)
        
        # Normalization thresholds
        normalization = self.config.get("normalization", {})
        self.latency_max_ms: float = normalization.get("latency_max_ms", 100.0)
        self.jitter_max_ms: float = normalization.get("jitter_max_ms", 20.0)
        self.bandwidth_ref_mbps: float = normalization.get("bandwidth_ref_mbps", 1000.0)
        
        # Backward compatibility warning for deprecated cost_weights
        if "cost_weights" in self.config:
            _LOGGER.warning("cost_weights is deprecated, use weights_percent and normalization instead")

        neighbor_entries = self.config.get("neighbors", [])
        self.neighbor_settings: Dict[str, Dict[str, Any]] = {
            entry["id"]: entry for entry in neighbor_entries
        }

        # Estado por vizinho (direto)
        self.neighbors: Dict[str, Dict[str, Any]] = {}
        # Subnets locais (diretamente conectadas) deste roteador
        self.local_prefixes: List[str] = []
        # LSDB simplificada de subnets por roteador {router_id: set(prefixes)}
        self.lsdb_prefixes: Dict[str, Set[str]] = {}

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
            # Deriva prefixo da subnet /24 a partir do IP do link
            ip = settings["ip"]
            base = ".".join(ip.split(".")[:3]) + ".0/24"
            if base not in self.local_prefixes:
                self.local_prefixes.append(base)

        # Cada roteador deve "anunciar" suas subnets locais. Coloca as minhas na LSDB
        self.lsdb_prefixes[self.router_id] = set(self.local_prefixes)

        # Grafo de custos entre roteadores (router_id -> {neighbor_id: cost})
        self.topology_graph: Dict[str, Dict[str, float]] = {self.router_id: {}}
        # Tabela de roteamento lógico: destino(router_id) -> next_hop(router_id)
        self.routing_table: Dict[str, str] = {}
        # Rotas instaladas no kernel: prefix -> next_hop_ip
        self.installed_routes: Dict[str, str] = {}
        # Controle de versão de LSA por originador
        self.lsa_versions: Dict[str, int] = {}

        self._state_lock = threading.Lock()
        self._running = threading.Event()
        self._running.set()

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.listen_ip, self.listen_port))
        self._socket.settimeout(1.0)

        self._threads: list[threading.Thread] = []
        self._seqnum = 0  # meu seqnum de LSA

    # ------------------------------------------------------------------
    # Lifecycle management
    # ------------------------------------------------------------------
    def start(self) -> None:
        _LOGGER.info("Starting OSPF-Gaming daemon for router %s", self.router_id)

        self._threads = [
            threading.Thread(target=self._hello_loop, name="hello", daemon=True),
            threading.Thread(target=self._listen_loop, name="listener", daemon=True),
            threading.Thread(target=self._metric_loop, name="metrics", daemon=True),
            threading.Thread(target=self._dead_interval_loop, name="dead", daemon=True),
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
                payload, (source_ip, _) = self._socket.recvfrom(65535)
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

    def _dead_interval_loop(self) -> None:
        """Marca vizinhos como down se não recebem Hello por DEAD_INTERVAL."""
        while self._running.is_set():
            now = time.time()
            removed_any = False
            with self._state_lock:
                for nid, state in self.neighbors.items():
                    if now - state["last_hello"] > DEAD_INTERVAL:
                        state["metrics"]["cost"] = float("inf")
                        # Remove arestas do grafo ao expirar Hello
                        if self.topology_graph.get(self.router_id, {}).get(nid) is not None:
                            del self.topology_graph[self.router_id][nid]
                            removed_any = True
                        if self.topology_graph.get(nid, {}).get(self.router_id) is not None:
                            del self.topology_graph[nid][self.router_id]
                            removed_any = True
            # Recalcula rotas e dispara LSA imediatamente após remoção de links mortos
            if removed_any:
                self._recalculate_routes()
                self._broadcast_lsa()  # Acelera convergência enviando LSA imediatamente
            time.sleep(5)

    # ------------------------------------------------------------------
    # Packet handlers
    # ------------------------------------------------------------------
    def _process_hello(self, message: Dict[str, Any], source_ip: str) -> None:
        neighbor_id = message.get("router_id")
        if neighbor_id == self.router_id:
            return
        if neighbor_id not in self.neighbors:
            # não aceitamos "novos" vizinhos dinâmicos neste protótipo
            return

        with self._state_lock:
            self.neighbors[neighbor_id]["last_hello"] = time.time()
            # se JSON não tinha IP, grava o source_ip observado
            self.neighbors[neighbor_id].setdefault("ip", source_ip)

    def _process_lsa(self, message: Dict[str, Any]) -> None:
        origin = message.get("router_id")
        links = message.get("neighbors", {})
        prefixes = message.get("prefixes", [])
        seqnum = int(message.get("seqnum", 0))
        hops = int(message.get("hops", 0))
        if not origin:
            return

        need_reflood = False
        topology_changed = False
        with self._state_lock:
            current_seq = self.lsa_versions.get(origin, -1)
            if seqnum <= current_seq:
                return

            self.lsa_versions[origin] = seqnum

            # Garante estrutura
            if origin not in self.topology_graph:
                self.topology_graph[origin] = {}

            before_links = dict(self.topology_graph[origin])

            # Aplica custos anunciados
            new_links = {k: float(v) for k, v in links.items()}
            self.topology_graph[origin].update(new_links)

            # Remove links que desapareceram do LSA (stale)
            for old_neighbor in list(self.topology_graph[origin].keys()):
                if old_neighbor not in new_links:
                    del self.topology_graph[origin][old_neighbor]

            if self.topology_graph[origin] != before_links:
                need_reflood = True
                topology_changed = True

            # Prefixes
            old_prefixes = self.lsdb_prefixes.get(origin, set())
            new_prefixes = {p for p in prefixes if isinstance(p, str)}
            if new_prefixes and new_prefixes != old_prefixes:
                self.lsdb_prefixes[origin] = new_prefixes
                need_reflood = True
                topology_changed = True

        if topology_changed:
            # Recalcula rotas imediatamente para acelerar convergência
            self._recalculate_routes()

        if need_reflood and hops > 0:
            message["hops"] = hops - 1
            for neighbor_id in list(self.neighbors.keys()):
                if neighbor_id != origin:
                    self._send_message(neighbor_id, message)

    # ------------------------------------------------------------------
    # Metrics and topology management
    # ------------------------------------------------------------------
    def _update_link_metrics(self) -> None:
        # Cria cópia protegida por lock para evitar condição de corrida com dead interval loop
        with self._state_lock:
            neighbors_copy = dict(self.neighbors)
        
        for neighbor_id, neighbor in neighbors_copy.items():
            ip_address = neighbor.get("ip")
            if not ip_address:
                continue

            latency, jitter, loss = measure_link_quality(
                ip_address, self.ping_count, self.ping_interval
            )
            bandwidth = neighbor["metrics"].get("bandwidth")
            if bandwidth is None:
                bandwidth = get_static_bandwidth(self.router_id, neighbor_id)
                neighbor["metrics"]["bandwidth"] = bandwidth

            cost = self._calculate_cost(latency, jitter, loss, bandwidth)

            with self._state_lock:
                # Verifica se vizinho ainda existe (pode ter sido removido pelo dead interval)
                if neighbor_id in self.neighbors:
                    self.neighbors[neighbor_id]["metrics"].update(
                        {
                            "latency": latency,
                            "jitter": jitter,
                            "loss": loss,
                            "cost": cost,
                        }
                    )
                    # bidirecional (simplificação)
                    self.topology_graph.setdefault(self.router_id, {})[neighbor_id] = cost
                    self.topology_graph.setdefault(neighbor_id, {})[self.router_id] = cost

            _LOGGER.debug(
                "Metrics for %s -> latency: %.2f ms, jitter: %.3f ms, loss: %.2f%%, cost: %.2f",
                neighbor_id,
                latency,
                jitter,
                loss,
                cost,
            )

    def _calculate_cost(
        self, latency: float, jitter: float, loss: float, bandwidth: Optional[int]
    ) -> float:
        if not math.isfinite(latency) or not math.isfinite(jitter) or loss >= 100.0:
            return float("inf")
        
        # Normalize metrics to 0..1 range
        lat_norm = min(1.0, max(0.0, latency / self.latency_max_ms))
        jit_norm = min(1.0, max(0.0, jitter / self.jitter_max_ms))
        loss_norm = min(1.0, max(0.0, loss / 100.0))
        
        if bandwidth and bandwidth > 0:
            bw_norm = 1.0 - min(1.0, max(0.0, bandwidth / self.bandwidth_ref_mbps))
        else:
            bw_norm = 1.0  # No bandwidth = worst case
            
        # Calculate weighted components (percentages sum to 100)
        latency_component = lat_norm * self.weight_latency
        jitter_component = jit_norm * self.weight_jitter
        loss_component = loss_norm * self.weight_loss
        bandwidth_component = bw_norm * self.weight_bandwidth
        
        cost = latency_component + jitter_component + loss_component + bandwidth_component
        
        _LOGGER.debug(
            "Cost calculation: lat=(%.2f/%.1f)=%.2f*%.0f=%.2f, jit=(%.2f/%.1f)=%.2f*%.0f=%.2f, loss=(%.2f/100)=%.2f*%.0f=%.2f, bw=(1-%.2f)=%.2f*%.0f=%.2f, total=%.2f",
            latency, self.latency_max_ms, lat_norm, self.weight_latency, latency_component,
            jitter, self.jitter_max_ms, jit_norm, self.weight_jitter, jitter_component,
            loss, loss_norm, self.weight_loss, loss_component,
            bandwidth/self.bandwidth_ref_mbps if bandwidth else 0, bw_norm, self.weight_bandwidth, bandwidth_component,
            cost
        )
        
        return cost

    def _broadcast_lsa(self) -> None:
        with self._state_lock:
            local_view = deepcopy(self.topology_graph.get(self.router_id, {}))
            prefixes = list(self.lsdb_prefixes.get(self.router_id, set())) or list(self.local_prefixes)
            self._seqnum += 1
            message = {
                "type": "lsa",
                "router_id": self.router_id,
                "neighbors": local_view,
                "seqnum": self._seqnum,
                "timestamp": time.time(),
                "prefixes": prefixes,  # 🔹 anuncio das minhas subnets
                "hops": LSA_TTL_HOPS,  # TTL inicial para controle de flood
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
        """
        Instala rotas para todas as subnets remotas anunciadas na LSDB.
        - Ignora prefixos locais (kernel já tem proto kernel scope link).
        - Remove somente rotas que não são mais alcançáveis segundo a tabela nova.
        """
        desired_prefixes: Dict[str, str] = {}  # prefix -> next_hop_ip

        # Para cada destino (router_id) com next-hop definido, instala as suas subnets anunciadas
        for destination, next_hop_id in new_routes.items():
            if destination == self.router_id:
                continue

            next_hop_ip = self._resolve_next_hop_ip(next_hop_id)
            if not next_hop_ip:
                continue

            for prefix in self._resolve_router_prefixes(destination):
                # não instala rota para subnets locais
                if prefix in self.local_prefixes:
                    continue
                desired_prefixes[prefix] = next_hop_ip

        # Adiciona/atualiza rotas necessárias
        for prefix, next_hop_ip in desired_prefixes.items():
            current_next_hop = self.installed_routes.get(prefix)
            if current_next_hop == next_hop_ip:
                continue
            try:
                if current_next_hop:
                    delete_route(prefix)
                add_route(prefix, next_hop_ip)  # se teu route_manager aceitar, pode passar a interface também
                self.installed_routes[prefix] = next_hop_ip
                _LOGGER.debug("Installed/updated route %s via %s", prefix, next_hop_ip)
            except Exception:
                _LOGGER.exception("Falha ao instalar rota para %s via %s", prefix, next_hop_ip)

        # Remove rotas que não são mais desejadas
        desired_set = set(desired_prefixes.keys())
        for prefix in list(self.installed_routes.keys()):
            if prefix not in desired_set:
                # Evita remover prefixo local por engano (não deveria estar em installed_routes, mas por segurança)
                if prefix in self.local_prefixes:
                    self.installed_routes.pop(prefix, None)
                    continue
                self._remove_installed_route(prefix)

    def _remove_installed_route(self, prefix: str) -> None:
        if prefix not in self.installed_routes:
            return
        try:
            delete_route(prefix)
        except Exception:
            _LOGGER.exception("Falha ao remover rota para %s", prefix)
        else:
            self.installed_routes.pop(prefix, None)
            _LOGGER.debug("Removed route %s", prefix)

    def _resolve_next_hop_ip(self, next_hop_id: str) -> Optional[str]:
        neighbor = self.neighbors.get(next_hop_id)
        if neighbor:
            return neighbor.get("ip")
        return None

    def _resolve_router_prefixes(self, router_id: str) -> List[str]:
        """Retorna subnets diretamente conectadas conhecidas para 'router_id' da LSDB."""
        if router_id == self.router_id:
            return list(self.local_prefixes)
        # Primeiro tenta LSDB (vinha pelos LSAs processados)
        prefixes = self.lsdb_prefixes.get(router_id)
        if prefixes:
            return list(prefixes)
        # Fallback: se é vizinho direto e não vimos LSA de prefixes ainda, infere a /24 do link
        if router_id in self.neighbor_settings:
            ip = self.neighbor_settings[router_id]["ip"]
            base = ".".join(ip.split(".")[:3]) + ".0/24"
            return [base]
        return []

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
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to JSON config")
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

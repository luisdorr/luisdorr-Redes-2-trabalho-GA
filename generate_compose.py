from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable

try:
    import yaml
except ModuleNotFoundError as exc:
    raise SystemExit("PyYAML is required. Install it with 'pip install pyyaml'.") from exc

CONFIG_DIR = Path("config")
OUTPUT_OSPF = Path("docker-compose.yml")
OUTPUT_FRR = Path("docker-compose.frr.yml")
FRR_DAEMONS = Path("configs/daemons")


def network_name(a: str, b: str) -> str:
    return f"net_{min(a, b)}_{max(a, b)}"


def infer_subnet(ipv4: str) -> str:
    octets = ipv4.split(".")
    if len(octets) != 4:
        raise ValueError(f"invalid IPv4 address: {ipv4}")
    return ".".join(octets[:3]) + ".0/24"


def local_address_from_neighbor(ipv4: str) -> str:
    octets = ipv4.split(".")
    host = int(octets[3])
    if host == 2:
        host = 3
    elif host == 3:
        host = 2
    octets[3] = str(host)
    return ".".join(octets)


def load_router_configs() -> Dict[str, dict]:
    routers: Dict[str, dict] = {}
    for path in sorted(CONFIG_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        routers[data["router_id"]] = data | {"_filename": path.name}
    if not routers:
        raise SystemExit("no router configurations found under config/")
    return routers


def ensure_daemons_file() -> None:
    FRR_DAEMONS.parent.mkdir(parents=True, exist_ok=True)
    if FRR_DAEMONS.exists():
        return
    FRR_DAEMONS.write_text(
        """zebra=yes
ospfd=yes
staticd=no
bgpd=no
ripd=no
isisd=no
""",
        encoding="utf-8",
    )


def attach_networks(
    global_networks: Dict[str, dict],
    service_networks: Dict[str, dict],
    router_id: str,
    neighbors: Iterable[dict],
) -> None:
    for neighbor in neighbors:
        peer = neighbor["id"]
        net = network_name(router_id, peer)
        subnet = infer_subnet(neighbor["ip"])
        global_networks.setdefault(
            net,
            {"driver": "bridge", "ipam": {"config": [{"subnet": subnet}]}}
        )
        service_networks[net] = {"ipv4_address": local_address_from_neighbor(neighbor["ip"])}


def build_gaming_compose(configs: Dict[str, dict]) -> dict:
    networks: Dict[str, dict] = {}
    services: Dict[str, dict] = {}
    for router_id, data in configs.items():
        service_networks: Dict[str, dict] = {}
        attach_networks(networks, service_networks, router_id, data.get("neighbors", []))
        services[router_id] = {
            "image": "frrouting/frr:latest",
            "container_name": router_id,
            "privileged": True,
            "cap_add": ["NET_ADMIN"],
            "volumes": ["./:/opt/ospf-gaming"],
            "command": [
                "python3",
                "/opt/ospf-gaming/ospf_gaming_daemon.py",
                "--config",
                f"/opt/ospf-gaming/config/{data['_filename']}",
            ],
            "networks": service_networks,
        }
    return {"version": "3.9", "services": services, "networks": networks}


def build_frr_compose(configs: Dict[str, dict]) -> dict:
    networks: Dict[str, dict] = {}
    services: Dict[str, dict] = {}
    for router_id, data in configs.items():
        service_networks: Dict[str, dict] = {}
        attach_networks(networks, service_networks, router_id, data.get("neighbors", []))
        services[router_id] = {
            "image": "frrouting/frr:latest",
            "container_name": router_id,
            "privileged": True,
            "cap_add": ["NET_ADMIN"],
            "volumes": [
                f"./configs/{router_id}.conf:/etc/frr/frr.conf",
                "./configs/daemons:/etc/frr/daemons",
            ],
            "networks": service_networks,
        }
    return {"version": "3.9", "services": services, "networks": networks}


def emit_yaml(document: dict, target: Path) -> None:
    target.write_text(yaml.dump(document, sort_keys=False), encoding="utf-8")
    print(f"wrote {target}")


def main() -> None:
    ensure_daemons_file()
    configs = load_router_configs()
    emit_yaml(build_gaming_compose(configs), OUTPUT_OSPF)
    emit_yaml(build_frr_compose(configs), OUTPUT_FRR)


if __name__ == "__main__":
    main()

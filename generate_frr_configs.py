from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Set

CONFIG_DIR = Path("config")
OUTPUT_DIR = Path("configs")

FRR_TEMPLATE = """!
hostname {router}
password zebra
log stdout
!
router ospf
 router-id {router_id}
{networks}
!
line vty
!
"""


def infer_subnet(ipv4: str) -> str:
    octets = ipv4.split(".")
    if len(octets) != 4:
        raise ValueError(f"invalid IPv4 address: {ipv4}")
    return ".".join(octets[:3]) + ".0/24"


def load_router_configs() -> Dict[str, dict]:
    configs: Dict[str, dict] = {}
    for path in sorted(CONFIG_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        configs[payload["router_id"]] = payload
    if not configs:
        raise SystemExit("no router configurations found under config/")
    return configs


def collect_prefixes(router: str, data: dict) -> Set[str]:
    prefixes: Set[str] = set(data.get("local_prefixes", []))
    for neighbor in data.get("neighbors", []):
        prefixes.add(infer_subnet(neighbor["ip"]))
    return prefixes


def render_networks(prefixes: Iterable[str]) -> str:
    lines = [f" network {prefix} area 0" for prefix in sorted(prefixes)]
    return "\n".join(f" {line}" for line in lines)


def render_router_id(router: str) -> str:
    numeric = router.lstrip("r")
    return ".".join([numeric] * 4)


def write_configs(configs: Dict[str, dict]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for router, data in configs.items():
        prefixes = collect_prefixes(router, data)
        networks_block = render_networks(prefixes)
        content = FRR_TEMPLATE.format(
            router=router,
            router_id=render_router_id(router),
            networks=networks_block,
        )
        target = OUTPUT_DIR / f"{router}.conf"
        target.write_text(content, encoding="utf-8")
        print(f"wrote {target}")


def main() -> None:
    write_configs(load_router_configs())


if __name__ == "__main__":
    main()

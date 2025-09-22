import os
import json
import yaml

CONFIG_DIR = "config"
OUTPUT_FILE = "docker-compose.yml"

compose = {
    "services": {},
    "networks": {}
}

def network_name(r1, r2):
    return f"net_{min(r1,r2)}_{max(r1,r2)}"

for file in os.listdir(CONFIG_DIR):
    if not file.endswith(".json"):
        continue

    path = os.path.join(CONFIG_DIR, file)
    with open(path) as f:
        cfg = json.load(f)

    router_id = cfg["router_id"]
    service = {
        "image": "frrouting/frr:latest",
        "container_name": router_id,
        "privileged": True,
        "cap_add": ["NET_ADMIN"],
        "volumes": [
            "./:/opt/ospf-gaming"
        ],
        "command": f"python3 /opt/ospf-gaming/ospf_gaming_daemon.py --config /opt/ospf-gaming/config/{file}",
        "networks": {}
    }

    # Para cada vizinho, cria a rede ponto-a-ponto
    for neighbor in cfg.get("neighbors", []):
        net = network_name(router_id, neighbor["id"])
        ip = neighbor["ip"]

        # Descobre a subnet automaticamente (assume /30 ou /29 com base no .x)
        octets = ip.split(".")
        base = ".".join(octets[:3] + ["0"])
        subnet = f"{base}/24"

        compose["networks"].setdefault(net, {
            "driver": "bridge",
            "ipam": {
                "config": [{"subnet": subnet}]
            }
        })

        service["networks"][net] = {"ipv4_address": ip}

    compose["services"][router_id] = service

with open(OUTPUT_FILE, "w") as f:
    yaml.dump(compose, f, sort_keys=False)

print(f"[+] Docker Compose gerado em {OUTPUT_FILE}")

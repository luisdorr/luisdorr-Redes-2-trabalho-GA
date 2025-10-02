import os
import stat
import json
try:
    import yaml
except ModuleNotFoundError as exc:
    raise SystemExit(
        "PyYAML não está instalado. Instale com 'pip install pyyaml' e tente novamente."
    ) from exc

CONFIG_DIR = "config"
OUTPUT_FILE = "docker-compose.yml"
SCRIPTS_DIR = "scripts"

compose = {
    "services": {},
    "networks": {}
}


def network_name(r1, r2):
    """Nome de rede único para um par de roteadores (ordem consistente)."""
    return f"net_{min(r1, r2)}_{max(r1, r2)}"


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
            "./:/opt/ospf-gaming",
            "./scripts:/opt/ospf-gaming/scripts"
        ],
        # 👇 força todos os daemons a usarem DEBUG
        "command": f"python3 /opt/ospf-gaming/ospf_gaming_daemon.py "
                   f"--config /opt/ospf-gaming/config/{file} --log-level DEBUG",
        "networks": {},
        "healthcheck": {
            "test": [
                "CMD-SHELL",
                "pgrep -f 'python3 /opt/ospf-gaming/ospf_gaming_daemon.py' >/dev/null"
            ],
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "15s"
        }
    }

    # Para cada vizinho, cria uma rede ponto-a-ponto
    for neighbor in cfg.get("neighbors", []):
        net = network_name(router_id, neighbor["id"])
        ip = neighbor["ip"]

        # Assume /24 (pode ajustar para /30 se quiser mais enxuto)
        octets = ip.split(".")
        base = ".".join(octets[:3] + ["0"])
        subnet = f"{base}/24"

        compose["networks"].setdefault(net, {
            "driver": "bridge",
            "ipam": {
                "config": [{"subnet": subnet}]
            }
        })

        octets = ip.split(".")
        base = ".".join(octets[:3])
        last = int(octets[3])
        if last == 3:
            self_last = 2
        elif last == 2:
            self_last = 3
        else:
            self_last = last

        self_ip = f"{base}.{self_last}"
        service["networks"][net] = {"ipv4_address": self_ip}


    compose["services"][router_id] = service


os.makedirs(SCRIPTS_DIR, exist_ok=True)

def ensure_executable_scripts(directory: str) -> None:
    for entry in os.listdir(directory):
        path = os.path.join(directory, entry)
        if not os.path.isfile(path):
            continue
        if not entry.endswith(".sh"):
            continue
        current_mode = os.stat(path).st_mode
        new_mode = current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        if new_mode != current_mode:
            os.chmod(path, new_mode)


ensure_executable_scripts(SCRIPTS_DIR)

with open(OUTPUT_FILE, "w") as f:
    yaml.dump(compose, f, sort_keys=False)

print(f"[+] Docker Compose gerado em {OUTPUT_FILE}")

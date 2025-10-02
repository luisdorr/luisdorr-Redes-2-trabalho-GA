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
OUTPUT_FILE_GAMING = "docker-compose.yml"
OUTPUT_FILE_FRR = "docker-compose.frr.yml"
SCRIPTS_DIR = "scripts"

def network_name(r1, r2):
    """Nome de rede único para um par de roteadores (ordem consistente)."""
    return f"net_{min(r1, r2)}_{max(r1, r2)}"

# Estruturas bases
compose_gaming = {
    "version": "3.9",
    "services": {},
    "networks": {}
}
compose_frr = {
    "version": "3.9",
    "services": {},
    "networks": {}
}

# Garante que o arquivo de daemons exista
DAEMONS_FILE = "configs/daemons"
os.makedirs("configs", exist_ok=True)
if not os.path.exists(DAEMONS_FILE):
    with open(DAEMONS_FILE, "w") as f:
        f.write("zebra=yes\n")
        f.write("ospfd=yes\n")
        f.write("staticd=no\n")
        f.write("bgpd=no\n")
        f.write("ripd=no\n")
        f.write("isisd=no\n")

for file in os.listdir(CONFIG_DIR):
    if not file.endswith(".json"):
        continue

    path = os.path.join(CONFIG_DIR, file)
    with open(path) as f:
        cfg = json.load(f)

    router_id = cfg["router_id"]

    # -------------------
    # Serviço Gaming
    # -------------------
    service_gaming = {
        "image": "frrouting/frr:latest",
        "container_name": router_id,
        "privileged": True,
        "cap_add": ["NET_ADMIN"],
        "volumes": [
            "./:/opt/ospf-gaming",
            "./scripts:/opt/ospf-gaming/scripts"
        ],
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

    # -------------------
    # Serviço FRR (sem command/healthcheck, mas com frr.conf + daemons)
    # -------------------
    service_frr = {
        "image": "frrouting/frr:latest",
        "container_name": router_id,
        "privileged": True,
        "cap_add": ["NET_ADMIN"],
        "volumes": [
            f"./configs/{router_id}.conf:/etc/frr/frr.conf",
            "./configs/daemons:/etc/frr/daemons"
        ],
        "networks": {}
    }

    # Redes ponto-a-ponto
    for neighbor in cfg.get("neighbors", []):
        net = network_name(router_id, neighbor["id"])
        ip = neighbor["ip"]

        octets = ip.split(".")
        base = ".".join(octets[:3] + ["0"])
        subnet = f"{base}/24"

        for compose in (compose_gaming, compose_frr):
            compose["networks"].setdefault(net, {
                "driver": "bridge",
                "ipam": {
                    "config": [{"subnet": subnet}]
                }
            })

        base = ".".join(octets[:3])
        last = int(octets[3])
        if last == 3:
            self_last = 2
        elif last == 2:
            self_last = 3
        else:
            self_last = last
        self_ip = f"{base}.{self_last}"

        service_gaming["networks"][net] = {"ipv4_address": self_ip}
        service_frr["networks"][net] = {"ipv4_address": self_ip}

    compose_gaming["services"][router_id] = service_gaming
    compose_frr["services"][router_id] = service_frr


# Garantir que os scripts são executáveis
def ensure_executable_scripts(directory: str) -> None:
    if not os.path.exists(directory):
        return
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

# Escrever os dois docker-compose
with open(OUTPUT_FILE_GAMING, "w") as f:
    yaml.dump(compose_gaming, f, sort_keys=False)

with open(OUTPUT_FILE_FRR, "w") as f:
    yaml.dump(compose_frr, f, sort_keys=False)

print(f"[+] Docker Compose (OSPF-Gaming) gerado em {OUTPUT_FILE_GAMING}")
print(f"[+] Docker Compose (FRR/OSPF padrão) gerado em {OUTPUT_FILE_FRR}")

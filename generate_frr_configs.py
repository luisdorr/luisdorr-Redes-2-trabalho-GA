import os

# Define aqui a topologia por roteador
# Cada router: lista de redes diretamente conectadas
TOPOLOGY = {
    "r1": ["10.0.12.0/24", "10.0.13.0/24"],
    "r2": ["10.0.12.0/24", "10.0.23.0/24", "10.0.24.0/24"],
    "r3": ["10.0.13.0/24", "10.0.23.0/24", "10.0.35.0/24"],
    "r4": ["10.0.24.0/24", "10.0.48.0/24", "10.0.46.0/24"],
    "r5": ["10.0.35.0/24", "10.0.58.0/24", "10.0.57.0/24"],
    "r6": ["10.0.46.0/24"],
    "r7": ["10.0.57.0/24"],
    "r8": ["10.0.48.0/24", "10.0.58.0/24"],
}

OUTPUT_DIR = "configs"

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

def generate_configs():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for router, networks in TOPOLOGY.items():
        # redes com indentação correta
        networks_str = "\n".join([f" network {net} area 0" for net in networks])
        # router-id no formato IPv4 (1.1.1.1, 2.2.2.2, etc.)
        rid_num = router[1:]  # pega só o número (ex: "1" de "r1")
        router_id = f"{rid_num}.{rid_num}.{rid_num}.{rid_num}"

        # garantir indentação
        networks_str = "\n".join([f" {line}" for line in networks_str.splitlines()])

        conf = FRR_TEMPLATE.format(router=router, router_id=router_id, networks=networks_str)

        out_path = os.path.join(OUTPUT_DIR, f"{router}.conf")
        with open(out_path, "w") as f:
            f.write(conf)

        print(f"Generated {out_path}")


if __name__ == "__main__":
    generate_configs()

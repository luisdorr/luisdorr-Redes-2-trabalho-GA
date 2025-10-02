# OSPF-Gaming

OSPF-Gaming is a QoS-aware link-state routing daemon for educational labs. Each router floods latency, jitter, loss, and static bandwidth observations, and the control plane maps the link-state database into Layer-3 next-hop decisions with Dijkstra.

## Quick Start
- Install Docker, Docker Compose, and Python 3.11 with `pip install pandas matplotlib PyYAML` on the host.
- Generate the lab assets:
  - `python generate_frr_configs.py`
  - `python generate_compose.py`
- Run the automated comparison (`analysis.py` spins the topologies, measures pings r1->r5, degrades r3, and stores CSV/plots):
  - `python analysis.py`

## What the Experiment Does
- Spins up the eight OSPF-Gaming routers and the FRR/OSPF baseline using the compose files in the project root.
- Records baseline ICMP latency, jitter, and loss between r1 and r5.
- Injects delay and loss on `r3:eth0` with `tc netem` to penalise the direct path.
- Tracks the time until r1 installs a new next-hop for 10.0.35.3, capturing convergence for both protocols.
- Repeats the ping measurements after convergence and stores raw outputs under `results/raw/<protocol>/`.

## Outputs
- Tables under `results/tables/` (`latency_jitter_loss.csv`, `convergence.csv`).
- Figures under `results/figures/` (`latency_jitter.png`, `convergence.png`) when `matplotlib` is available.
- Generated FRR configs live in `configs/`, while Docker manifests are `docker-compose.yml` (OSPF-Gaming) and `docker-compose.frr.yml` (FRR baseline).

## Manual Lab Control
- Launch OSPF-Gaming only: `docker compose -f docker-compose.yml up -d`
- Launch FRR baseline: `docker compose -f docker-compose.frr.yml up -d`
- Remove impairment manually: `docker exec r3 tc qdisc del dev eth0 root`
- Tear down a topology: `docker compose -f <file> down -v`

## Code Map
- `algorithm.py` — shortest-path computation returning next hops and costs.
- `metrics.py` — active ICMP probing plus QoS cost normalisation.
- `ospf_gaming_daemon.py` — Hello/LSA flooding, QoS weighting, and kernel FIB sync.
- `route_manager.py` — thin wrapper around `ip route` for FIB updates.
- `analysis.py` — automation for the r1->r5 convergence experiment.
- `generate_compose.py` / `generate_frr_configs.py` — regenerate topology manifests from `config/*.json`.

## Reporting
- After running `analysis.py`, reference the CSV files and plots in your presentation or course report.
- For live inspection inside the lab, `docker exec r1 ip route get 10.0.35.3` shows the current next hop selected by the QoS-aware control plane.


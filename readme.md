# OSPF-Gaming

OSPF-Gaming is a proof-of-concept, QoS-aware link-state routing protocol tuned for interactive traffic such as online gaming. The daemon extends traditional OSPF concepts with active latency, jitter, packet loss, and static bandwidth measurements so that the best next-hop is calculated with end-to-end quality in mind.

## Repository structure

| File | Description |
| ---- | ----------- |
| `docker-compose.yml` | Docker Compose topology with eight FRRouting routers (r1-r8) and two end hosts (h1, h2) interconnected in a redundant mesh. |
| `ospf_gaming_daemon.py` | Multithreaded Python daemon that speaks the OSPF-Gaming protocol, maintains link-state information, and synchronises kernel routes. |
| `metrics.py` | Helper functions to collect QoS metrics via `ping` and a static bandwidth catalogue. |
| `algorithm.py` | Dijkstra-based shortest path implementation used to build routing tables. |
| `route_manager.py` | Wrapper around `ip route` to add and remove kernel forwarding entries. |
| `config/` | JSON configuration files for every router (`config.json` is the template for `r1`). |
| `readme.md` | Original coursework documentation left untouched for reference. |

## Launching the virtual topology

1. Ensure Docker and Docker Compose are installed on the host system.
2. From the repository root directory, start the lab:
   ```bash
   docker-compose up -d
   ```
3. Verify that the containers are running:
   ```bash
   docker-compose ps
   ```
4. Tail the daemon logs for a given router (replace `r1` with any router ID) to ensure the process is healthy:
   ```bash
   docker logs -f r1
   ```

Every router container enables IP forwarding, boots FRRouting, and launches `ospf_gaming_daemon.py` with the configuration in `config/<router>.json`. The project directory is mounted into `/opt/ospf-gaming` inside each router container so any edits made locally are reflected immediately.

## Configuring the OSPF-Gaming daemon

The repository ships ready-to-use configurations for all routers under `config/r1.json` through `config/r8.json`. IP addresses deliberately avoid the `.1` host address on each /29 link so that the daemon always peers with the `.2`/`.3` endpoints defined in `docker-compose.yml`.

To adjust the lab for a different topology:

1. Update the relevant `config/<router>.json` files with the new neighbour IPs, UDP ports, and optional `route_mappings`.
2. Restart the affected router container so Docker Compose reloads the modified configuration, for example:
   ```bash
   docker-compose restart r3
   ```

The daemon running inside each router will:
- send Hello packets on a dedicated UDP port to discover peers,
- listen for Hello and LSA messages on another thread,
- periodically measure latency, jitter, and packet loss via `ping`, combine the results with static bandwidth values, and run Dijkstra's algorithm to update routes.

### Route management

The daemon optionally manages kernel routes when `route_mappings` are specified in the configuration. Each entry maps a remote router ID to a destination prefix (and optionally an egress interface). When present, the daemon installs or withdraws Linux routes via `ip route add`/`ip route del`. Omitting `route_mappings` leaves kernel routing untouched, which is useful during lab bring-up.

## Building a standalone container

The repository also ships a `Dockerfile` that bundles the daemon, helper modules, and the default configuration under `/opt/ospf-gaming`. Build and run it to launch a single router without Docker Compose:

```bash
docker build -t ospf-gaming .
docker run --rm --network host ospf-gaming
```

The container entrypoint executes `python3 ospf_gaming_daemon.py --config /opt/ospf-gaming/config/config.json`. Mount a directory with customised configurations if you need to override the default example:

```bash
docker run --rm --network host \
  -v "$(pwd)/config:/opt/ospf-gaming/config" \
  ospf-gaming
```

## Metric collection details

`metrics.py` issues `ping -c 10 -i 0.2` towards each neighbour. The resulting average RTT, jitter (mdev), and packet loss percentage are parsed from the command's summary output. Static bandwidth values are stored in `STATIC_BANDWIDTH` to avoid resource-intensive throughput tests; adjust them to match the expected link capacities in your topology.

## Next steps

This repository establishes the groundwork for further protocol development. Future enhancements may include LSA ageing, reliable flooding, topology persistence, priority queues for gaming flows, and integration with FRRouting's zebra for interface discovery.

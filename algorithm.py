"""Core routing algorithm for the OSPF-Gaming protocol."""

from __future__ import annotations

import heapq
from typing import Dict, Hashable, Tuple

Graph = Dict[Hashable, Dict[Hashable, float]]
RoutingTable = Dict[Hashable, Hashable]


def calculate_shortest_paths(graph: Graph, start_node: Hashable) -> RoutingTable:
    """Compute next-hop routing decisions using Dijkstra's algorithm.

    Parameters
    ----------
    graph:
        Representation of the network topology where keys are node identifiers
        and values are dictionaries mapping neighbour nodes to edge weights.
    start_node:
        Identifier of the local router.

    Returns
    -------
    dict
        Mapping between destination nodes and their next hop from the perspective
        of ``start_node``.
    """

    if start_node not in graph:
        return {}

    distances: Dict[Hashable, float] = {node: float("inf") for node in graph}
    previous: Dict[Hashable, Hashable | None] = {node: None for node in graph}
    distances[start_node] = 0.0

    priority_queue: list[Tuple[float, Hashable]] = [(0.0, start_node)]
    visited: set[Hashable] = set()

    while priority_queue:
        current_distance, current_node = heapq.heappop(priority_queue)
        if current_node in visited:
            continue
        visited.add(current_node)

        for neighbour, weight in graph.get(current_node, {}).items():
            distance_via_current = current_distance + weight
            if distance_via_current < distances.get(neighbour, float("inf")):
                distances[neighbour] = distance_via_current
                previous[neighbour] = current_node
                heapq.heappush(priority_queue, (distance_via_current, neighbour))

    routing_table: RoutingTable = {}
    for destination in graph:
        if destination == start_node:
            continue

        next_hop = _first_hop(previous, destination)
        if next_hop is not None:
            routing_table[destination] = next_hop

    return routing_table


def _first_hop(previous: Dict[Hashable, Hashable | None], destination: Hashable) -> Hashable | None:
    """Return the first hop on the path to ``destination`` if reachable."""

    path: list[Hashable] = []
    current = destination

    while current is not None:
        path.append(current)
        current = previous.get(current)

    if not path:
        return None

    path.reverse()
    if len(path) < 2:
        return None

    return path[1]


__all__ = ["calculate_shortest_paths"]

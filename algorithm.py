from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Dict, Hashable, Optional

Graph = Dict[Hashable, Dict[Hashable, float]]


@dataclass(frozen=True, slots=True)
class PathInfo:
    """First hop and accumulated cost for a destination in the link-state domain."""

    next_hop: Hashable
    cost: float


RoutingTable = Dict[Hashable, PathInfo]


def calculate_shortest_paths(graph: Graph, origin: Hashable) -> RoutingTable:
    """Produce Layer-3 forwarding choices from the link-state graph via Dijkstra."""

    if origin not in graph:
        return {}

    distances: Dict[Hashable, float] = {node: math.inf for node in graph}
    predecessors: Dict[Hashable, Optional[Hashable]] = {node: None for node in graph}
    distances[origin] = 0.0

    queue: list[tuple[float, Hashable]] = [(0.0, origin)]
    visited: set[Hashable] = set()

    while queue:
        current_distance, node = heapq.heappop(queue)
        if node in visited:
            continue
        visited.add(node)

        for neighbor, weight in graph.get(node, {}).items():
            if not math.isfinite(weight):
                continue
            candidate = current_distance + weight
            if candidate < distances.get(neighbor, math.inf):
                distances[neighbor] = candidate
                predecessors[neighbor] = node
                heapq.heappush(queue, (candidate, neighbor))

    table: RoutingTable = {}
    for destination, total_cost in distances.items():
        if destination == origin or not math.isfinite(total_cost):
            continue
        next_hop = _trace_first_hop(predecessors, destination)
        if next_hop is not None:
            table[destination] = PathInfo(next_hop=next_hop, cost=total_cost)

    return table


def _trace_first_hop(predecessors: Dict[Hashable, Optional[Hashable]], destination: Hashable) -> Optional[Hashable]:
    node: Optional[Hashable] = destination
    path: list[Hashable] = []
    while node is not None:
        path.append(node)
        node = predecessors.get(node)

    if len(path) < 2:
        return None

    return path[-2]


__all__ = ["PathInfo", "calculate_shortest_paths"]

"""Topology queries over a board context."""

from __future__ import annotations

from collections import deque
from typing import Any

from .data import BoardContext


class Topology:
    def __init__(self, board: BoardContext) -> None:
        self.board = board

    def list_nets(self, domain: str | None = None, risk_level: str | None = None) -> list[dict[str, Any]]:
        nets = []
        for net in self.board.nets.values():
            if domain and net.get("domain") != domain:
                continue
            if risk_level and net.get("risk_level") != risk_level:
                continue
            nets.append(
                {
                    "name": net["name"],
                    "aliases": net.get("aliases", []),
                    "domain": net.get("domain", "unknown"),
                    "risk_level": net.get("risk_level", "low"),
                    "expected_voltage": net.get("expected_voltage"),
                    "expected_frequency": net.get("expected_frequency"),
                    "test_points": [point["id"] for point in self.board.test_points.values() if point["net"] == net["name"]],
                }
            )
        return sorted(nets, key=lambda item: item["name"])

    def trace_net_neighbors(self, net: str, depth: int = 1) -> dict[str, Any]:
        start = self.board.canonical_net(net)
        max_depth = max(0, min(depth, 4))
        direct_components = self.components_on_net(start)
        direct_test_points = [point for point in self.board.test_points.values() if point["net"] == start]
        direct_rails = [
            rail for rail in self.board.rails.values() if rail.get("source_net") == start or rail.get("output_net") == start
        ]
        distances = self._net_distances(start, max_depth)
        neighbor_nets = [
            {"name": name, "distance": distance, "net": self.board.nets[name]}
            for name, distance in sorted(distances.items(), key=lambda item: (item[1], item[0]))
            if name != start
        ]
        return {
            "net": start,
            "depth": max_depth,
            "net_info": self.board.nets[start],
            "components": direct_components,
            "test_points": direct_test_points,
            "rails": direct_rails,
            "neighbor_nets": neighbor_nets,
        }

    def components_on_net(self, net: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for component in self.board.components.values():
            pins = [pin for pin in component.get("pins", []) if pin.get("net") == net]
            if pins:
                result.append(
                    {
                        "designator": component["designator"],
                        "type": component["type"],
                        "value": component.get("value"),
                        "part_number": component.get("part_number"),
                        "pins": pins,
                    }
                )
        return result

    def _net_distances(self, start: str, max_depth: int) -> dict[str, int]:
        distances = {start: 0}
        queue: deque[str] = deque([start])
        while queue:
            current = queue.popleft()
            if distances[current] >= max_depth:
                continue
            for neighbor in self._adjacent_nets(current):
                if neighbor not in distances:
                    distances[neighbor] = distances[current] + 1
                    queue.append(neighbor)
        return distances

    def _adjacent_nets(self, net: str) -> set[str]:
        neighbors: set[str] = set()
        for component in self.board.components.values():
            component_nets = {pin["net"] for pin in component.get("pins", [])}
            if net in component_nets:
                neighbors.update(component_nets - {net})
        for rail in self.board.rails.values():
            if rail.get("source_net") == net and rail.get("output_net") in self.board.nets:
                neighbors.add(rail["output_net"])
            if rail.get("output_net") == net and rail.get("source_net") in self.board.nets:
                neighbors.add(rail["source_net"])
            for key in ("enable_net", "power_good_net"):
                if rail.get(key) == net:
                    if rail.get("output_net") in self.board.nets:
                        neighbors.add(rail["output_net"])
                elif rail.get("output_net") == net and rail.get(key) in self.board.nets:
                    neighbors.add(rail[key])
        return neighbors


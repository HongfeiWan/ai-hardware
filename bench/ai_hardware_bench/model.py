"""Model adapter layer for bench-side diagnosis."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Protocol
import urllib.request

from .data import BoardContext, DiagnosticSession, utc_now
from .topology import Topology


class ModelAdapter(Protocol):
    id: str

    def analyze(self, board: BoardContext, session: DiagnosticSession, topology: Topology) -> dict[str, Any]:
        ...

    def status(self) -> dict[str, Any]:
        ...


@dataclass
class RuleBasedModelAdapter:
    id: str = "rule_based"

    def analyze(self, board: BoardContext, session: DiagnosticSession, topology: Topology) -> dict[str, Any]:
        evidence: list[str] = []
        related_nets: set[str] = set()
        low_voltage_rails: list[str] = []
        current_limited = False
        for measurement in session.data["measurements"]:
            target_net = measurement.get("target", {}).get("net")
            if target_net in board.nets:
                related_nets.add(target_net)
            features = measurement.get("features", {})
            if features.get("current_limited") or measurement.get("result", {}).get("current_limited"):
                current_limited = True
                evidence.append("Input rail reached the configured current limit.")
            if measurement.get("kind") == "waveform" and target_net in board.nets:
                expected = board.nets[target_net].get("expected_voltage")
                if expected and features.get("v_max_V") is not None and features["v_max_V"] < expected["min"]:
                    low_voltage_rails.append(target_net)
                    evidence.append(
                        f"{target_net} waveform peaks at {features['v_max_V']} V, below expected {expected['min']} V."
                    )
        if low_voltage_rails and current_limited:
            summary = f"{low_voltage_rails[0]} likely collapses because the upstream rail is current-limited."
            confidence = 0.72
            severity = "fault"
        elif low_voltage_rails:
            summary = f"{low_voltage_rails[0]} is below its expected voltage range."
            confidence = 0.58
            severity = "warning"
        else:
            summary = "No hard fault was identified from the available measurements."
            confidence = 0.35
            severity = "info"
            evidence.append("Available measurements remain within broad limits.")
        action_net = "SW_NODE" if "SW_NODE" in board.nets and low_voltage_rails else next(iter(board.nets))
        point = _first_point_for(board, action_net, "waveform")
        action = {
            "type": "measure_net",
            "net": action_net,
            "test_point": point.get("id") if point else None,
            "instrument_kind": "oscilloscope",
            "reason": "Check converter switching behavior before changing power conditions.",
            "risk_level": board.nets[action_net].get("risk_level", "low"),
            "requires_confirmation": board.nets[action_net].get("risk_level") == "high",
        }
        finding = {
            "id": f"finding_{len(session.data['findings']) + 1:03d}",
            "timestamp": utc_now(),
            "summary": summary,
            "confidence": confidence,
            "severity": severity,
            "evidence": evidence,
            "related_nets": sorted(related_nets | set(low_voltage_rails) | {action_net}),
            "related_components": [item["designator"] for item in topology.components_on_net(action_net)],
        }
        return {"finding": finding, "next_actions": [action]}

    def status(self) -> dict[str, Any]:
        return {"id": self.id, "backend": "rules", "requires_network": False}


@dataclass
class JsonHttpModelAdapter:
    """Generic HTTP JSON model adapter for local or private model gateways."""

    endpoint: str
    id: str = "json_http_model"
    timeout_s: float = 30.0

    def analyze(self, board: BoardContext, session: DiagnosticSession, topology: Topology) -> dict[str, Any]:
        body = json.dumps(
            {
                "board": board.data,
                "session": session.data,
                "topology": {
                    "nets": topology.list_nets(),
                    "rails": list(board.rails.values()),
                },
                "required_output": {
                    "finding": "diagnostic_session finding object",
                    "next_actions": "array of diagnostic_session next_action objects",
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        finding = payload.get("finding")
        actions = payload.get("next_actions")
        if not isinstance(finding, dict) or not isinstance(actions, list):
            raise RuntimeError("Model endpoint must return finding and next_actions")
        return {"finding": finding, "next_actions": actions}

    def status(self) -> dict[str, Any]:
        return {"id": self.id, "backend": "json_http", "endpoint": self.endpoint, "requires_network": True}


def build_model_adapter(config: dict[str, Any] | None) -> ModelAdapter:
    if not config or config.get("backend", "rules") == "rules":
        return RuleBasedModelAdapter(id=str(config.get("id", "rule_based")) if config else "rule_based")
    if config.get("backend") == "json_http":
        return JsonHttpModelAdapter(
            endpoint=str(config["endpoint"]),
            id=str(config.get("id", "json_http_model")),
            timeout_s=float(config.get("timeout_s", 30.0)),
        )
    raise ValueError(f"Unsupported model backend: {config.get('backend')}")


def _first_point_for(board: BoardContext, net: str, measurement: str) -> dict[str, Any] | None:
    for point in board.test_points.values():
        allowed = point.get("allowed_measurements") or []
        if point["net"] == net and (measurement in allowed or not allowed):
            return point
    return None


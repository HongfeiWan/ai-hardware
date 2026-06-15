"""Model adapter layer for bench-side diagnosis."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Protocol
import urllib.request

from .data import BoardContext, DiagnosticSession, utc_now
from .topology import Topology


FINDING_SEVERITIES = {"info", "warning", "fault", "critical"}
NEXT_ACTION_TYPES = {
    "measure_net",
    "probe_pin",
    "change_power_state",
    "set_fixture_state",
    "inspect_component",
    "ask_human",
    "stop",
}
RISK_LEVELS = {"low", "medium", "high"}


class ModelOutputValidationError(ValueError):
    """Raised when a model endpoint returns data outside the session schema."""


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
        over_voltage_rails: list[str] = []
        ripple_rails: list[str] = []
        low_enable_nets: list[str] = []
        low_power_good_nets: list[str] = []
        shorted_nets: list[str] = []
        inactive_switch_nodes: list[str] = []
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
                net_info = board.nets[target_net]
                expected = board.nets[target_net].get("expected_voltage")
                v_max = features.get("v_max_V")
                v_pp = features.get("v_pp_V")
                if _is_switching_node(net_info) and _waveform_inactive(v_max, v_pp):
                    inactive_switch_nodes.append(target_net)
                    evidence.append(
                        f"{target_net} switching waveform is inactive with v_pp={v_pp} V and v_max={v_max} V."
                    )
                if expected:
                    nominal = (float(expected["min"]) + float(expected["max"])) / 2.0
                    if v_max is not None and v_max < expected["min"]:
                        low_voltage_rails.append(target_net)
                        evidence.append(
                            f"{target_net} waveform peaks at {v_max} V, below expected {expected['min']} V."
                        )
                    if v_max is not None and v_max > float(expected["max"]) * 1.05:
                        over_voltage_rails.append(target_net)
                        evidence.append(
                            f"{target_net} waveform reaches {v_max} V, above expected {expected['max']} V."
                        )
                    if v_pp is not None and v_pp > max(0.15, nominal * 0.08):
                        ripple_rails.append(target_net)
                        evidence.append(
                            f"{target_net} ripple is {v_pp} Vpp, above the rule-of-thumb limit."
                        )
            if measurement.get("kind") == "dc_voltage" and target_net in board.nets:
                expected = board.nets[target_net].get("expected_voltage")
                voltage = features.get("voltage_V")
                if expected and voltage is not None and voltage < float(expected["min"]):
                    for rail in board.rails.values():
                        if rail.get("enable_net") == target_net:
                            low_enable_nets.append(target_net)
                            evidence.append(
                                f"{target_net} measures {voltage} V, below the enable threshold {expected['min']} V."
                            )
            if measurement.get("kind") == "logic" and target_net in board.nets:
                high_fraction = features.get("high_fraction")
                stuck_low = bool(features.get("stuck_low", False))
                if stuck_low or (isinstance(high_fraction, (int, float)) and high_fraction < 0.5):
                    for rail in board.rails.values():
                        if rail.get("power_good_net") == target_net:
                            low_power_good_nets.append(target_net)
                            evidence.append(
                                f"{target_net} logic capture remains low with high_fraction={high_fraction}."
                            )
            if measurement.get("kind") == "impedance" and target_net in board.nets:
                resistance = features.get("resistance_ohm")
                short_to_ground = bool(features.get("short_to_ground", False))
                if short_to_ground or (isinstance(resistance, (int, float)) and resistance < 10.0):
                    shorted_nets.append(target_net)
                    evidence.append(f"{target_net} measures {resistance} ohm to ground with power off.")
        if shorted_nets:
            action_net = shorted_nets[0]
            summary = f"{action_net} appears shorted to ground; do not apply power until the fault is isolated."
            confidence = 0.84
            severity = "critical"
            action = {
                "type": "stop",
                "reason": "Power-off impedance indicates a likely rail-to-ground short.",
                "risk_level": "high",
                "requires_confirmation": False,
            }
        elif inactive_switch_nodes:
            action_net = inactive_switch_nodes[0]
            summary = f"{action_net} is not switching; inspect the buck controller, enable path and input conditions."
            confidence = 0.68
            severity = "fault"
            component = _first_component_on_net(board, action_net)
            action = {
                "type": "inspect_component",
                "net": action_net,
                "component": component.get("designator") if component else None,
                "reason": "Switching node waveform lacks expected pulses; inspect the converter control and bootstrap path.",
                "risk_level": board.nets[action_net].get("risk_level", "low"),
                "requires_confirmation": board.nets[action_net].get("risk_level") == "high",
            }
        elif over_voltage_rails:
            summary = f"{over_voltage_rails[0]} is above its expected voltage range; stop power before further probing."
            confidence = 0.82
            severity = "critical"
            action = {
                "type": "stop",
                "reason": "Measured rail voltage exceeds the configured safe range.",
                "risk_level": "high",
                "requires_confirmation": False,
            }
            action_net = over_voltage_rails[0]
        elif low_enable_nets:
            action_net = low_enable_nets[0]
            affected_rails = [rail for rail in board.rails.values() if rail.get("enable_net") == action_net]
            affected_rail = affected_rails[0] if affected_rails else None
            summary = f"{action_net} is below its expected enable voltage; the downstream rail is likely disabled."
            confidence = 0.7
            severity = "fault"
            component = _first_component_on_net(board, action_net)
            action = {
                "type": "inspect_component",
                "net": action_net,
                "component": component.get("designator") if component else None,
                "reason": "Trace the enable source or pull network before probing the switching node.",
                "risk_level": board.nets[action_net].get("risk_level", "low"),
                "requires_confirmation": board.nets[action_net].get("risk_level") == "high",
            }
            if affected_rail:
                evidence.append(f"{action_net} controls rail {affected_rail['name']}.")
        elif low_power_good_nets:
            action_net = low_power_good_nets[0]
            affected_rails = [rail for rail in board.rails.values() if rail.get("power_good_net") == action_net]
            affected_rail = affected_rails[0] if affected_rails else None
            summary = f"{action_net} remains deasserted; the rail may be unhealthy or the power-good path is faulty."
            confidence = 0.66
            severity = "fault"
            component = _first_component_on_net(board, action_net)
            action = {
                "type": "inspect_component",
                "net": action_net,
                "component": component.get("designator") if component else None,
                "reason": "Check the regulator power-good pin, pull-up path and any downstream fault gating.",
                "risk_level": board.nets[action_net].get("risk_level", "low"),
                "requires_confirmation": board.nets[action_net].get("risk_level") == "high",
            }
            if affected_rail:
                evidence.append(f"{action_net} reports power-good for rail {affected_rail['name']}.")
        elif low_voltage_rails and current_limited:
            summary = f"{low_voltage_rails[0]} likely collapses because the upstream rail is current-limited."
            confidence = 0.72
            severity = "fault"
            action_net = "SW_NODE" if "SW_NODE" in board.nets else low_voltage_rails[0]
            action = _measure_waveform_action(board, action_net)
        elif low_voltage_rails:
            summary = f"{low_voltage_rails[0]} is below its expected voltage range."
            confidence = 0.58
            severity = "warning"
            action_net = "SW_NODE" if "SW_NODE" in board.nets else low_voltage_rails[0]
            action = _measure_waveform_action(board, action_net)
        elif ripple_rails:
            summary = f"{ripple_rails[0]} shows excessive ripple for its expected voltage range."
            confidence = 0.62
            severity = "warning"
            action_net = "SW_NODE" if "SW_NODE" in board.nets else ripple_rails[0]
            action = _measure_waveform_action(board, action_net)
        else:
            summary = "No hard fault was identified from the available measurements."
            confidence = 0.35
            severity = "info"
            evidence.append("Available measurements remain within broad limits.")
            action_net = next(iter(board.nets))
            action = _measure_waveform_action(board, action_net)
        finding = {
            "id": f"finding_{len(session.data['findings']) + 1:03d}",
            "timestamp": utc_now(),
            "summary": summary,
            "confidence": confidence,
            "severity": severity,
            "evidence": evidence,
            "related_nets": sorted(
                related_nets
                | set(low_voltage_rails)
                | set(over_voltage_rails)
                | set(ripple_rails)
                | set(low_enable_nets)
                | set(low_power_good_nets)
                | set(shorted_nets)
                | set(inactive_switch_nodes)
                | {action_net}
            ),
            "related_components": [item["designator"] for item in topology.components_on_net(action_net)],
        }
        return validate_model_output({"finding": finding, "next_actions": [action]}, board)

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
        return validate_model_output({"finding": finding, "next_actions": actions}, board)

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


def validate_model_output(payload: dict[str, Any], board: BoardContext) -> dict[str, Any]:
    """Validate the model response against the diagnostic session contract."""

    errors = validate_model_output_errors(payload, board)
    if errors:
        raise ModelOutputValidationError("Invalid model output: " + "; ".join(errors))
    return payload


def validate_model_output_errors(payload: dict[str, Any], board: BoardContext) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["model output must be an object"]
    finding = payload.get("finding")
    if not isinstance(finding, dict):
        errors.append("finding must be an object")
    else:
        _validate_finding(finding, errors, board)
    actions = payload.get("next_actions")
    if not isinstance(actions, list):
        errors.append("next_actions must be a list")
    else:
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                errors.append(f"next_actions[{index}] must be an object")
                continue
            _validate_next_action(action, errors, board, f"next_actions[{index}]")
    return errors


def _validate_finding(finding: dict[str, Any], errors: list[str], board: BoardContext) -> None:
    for field in ("id", "timestamp", "summary"):
        _require_non_empty_string(finding, field, errors, "finding")
    confidence = finding.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        errors.append("finding.confidence must be a number between 0 and 1")
    severity = finding.get("severity")
    if severity is not None and severity not in FINDING_SEVERITIES:
        errors.append(f"finding.severity must be one of {sorted(FINDING_SEVERITIES)}")
    _validate_string_list(finding, "evidence", errors, "finding")
    _validate_string_list(finding, "related_nets", errors, "finding")
    for net in finding.get("related_nets", []) or []:
        if isinstance(net, str) and net not in board.nets and net not in board.aliases:
            errors.append(f"finding.related_nets references unknown net {net}")
    _validate_string_list(finding, "related_components", errors, "finding")
    for component in finding.get("related_components", []) or []:
        if isinstance(component, str) and component not in board.components:
            errors.append(f"finding.related_components references unknown component {component}")


def _validate_next_action(action: dict[str, Any], errors: list[str], board: BoardContext, prefix: str) -> None:
    action_type = action.get("type")
    if action_type not in NEXT_ACTION_TYPES:
        errors.append(f"{prefix}.type must be one of {sorted(NEXT_ACTION_TYPES)}")
    _require_non_empty_string(action, "reason", errors, prefix)
    risk_level = action.get("risk_level")
    if risk_level not in RISK_LEVELS:
        errors.append(f"{prefix}.risk_level must be one of {sorted(RISK_LEVELS)}")
    requires_confirmation = action.get("requires_confirmation")
    if requires_confirmation is not None and not isinstance(requires_confirmation, bool):
        errors.append(f"{prefix}.requires_confirmation must be a boolean")
    net_name = action.get("net")
    canonical_net = None
    if net_name is not None:
        if not isinstance(net_name, str) or not net_name:
            errors.append(f"{prefix}.net must be a non-empty string")
        else:
            try:
                canonical_net = board.canonical_net(net_name)
            except ValueError:
                errors.append(f"{prefix}.net references unknown net {net_name}")
    test_point = action.get("test_point")
    if test_point is not None:
        if not isinstance(test_point, str) or not test_point:
            errors.append(f"{prefix}.test_point must be a non-empty string")
        elif test_point not in board.test_points:
            errors.append(f"{prefix}.test_point references unknown test point {test_point}")
        elif canonical_net and board.test_points[test_point].get("net") != canonical_net:
            errors.append(f"{prefix}.test_point {test_point} is not on net {canonical_net}")
    component = action.get("component")
    if component is not None and component not in board.components:
        errors.append(f"{prefix}.component references unknown component {component}")
    if canonical_net and board.nets[canonical_net].get("risk_level") == "high" and requires_confirmation is not True:
        errors.append(f"{prefix} targets high-risk net {canonical_net} without requires_confirmation=true")


def _require_non_empty_string(obj: dict[str, Any], field: str, errors: list[str], prefix: str) -> None:
    value = obj.get(field)
    if not isinstance(value, str) or not value:
        errors.append(f"{prefix}.{field} must be a non-empty string")


def _validate_string_list(obj: dict[str, Any], field: str, errors: list[str], prefix: str) -> None:
    value = obj.get(field)
    if value is None:
        return
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append(f"{prefix}.{field} must be a list of strings")


def _first_point_for(board: BoardContext, net: str, measurement: str) -> dict[str, Any] | None:
    for point in board.test_points.values():
        allowed = point.get("allowed_measurements") or []
        if point["net"] == net and (measurement in allowed or not allowed):
            return point
    return None


def _first_component_on_net(board: BoardContext, net: str) -> dict[str, Any] | None:
    for component in board.components.values():
        for pin in component.get("pins", []) or []:
            if pin.get("net") == net:
                return component
    return None


def _is_switching_node(net_info: dict[str, Any]) -> bool:
    name = str(net_info.get("name", "")).upper()
    notes = str(net_info.get("notes", "")).lower()
    return bool(net_info.get("expected_frequency")) or "SW" in name or "switching" in notes


def _waveform_inactive(v_max: Any, v_pp: Any) -> bool:
    if not isinstance(v_pp, (int, float)):
        return False
    if v_pp > 0.2:
        return False
    return not isinstance(v_max, (int, float)) or v_max < 0.5


def _measure_waveform_action(board: BoardContext, net: str) -> dict[str, Any]:
    point = _first_point_for(board, net, "waveform")
    risk_level = board.nets[net].get("risk_level", "low")
    return {
        "type": "measure_net",
        "net": net,
        "test_point": point.get("id") if point else None,
        "instrument_kind": "oscilloscope",
        "reason": "Check converter switching behavior before changing power conditions.",
        "risk_level": risk_level,
        "requires_confirmation": risk_level == "high",
    }

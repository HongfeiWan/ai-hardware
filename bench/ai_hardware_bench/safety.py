"""Safety policy checks and audit records for bench tool calls."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .data import BoardContext, utc_now


@dataclass
class SafetyDecision:
    allowed: bool
    risk_level: str = "low"
    requires_confirmation: bool = False
    reasons: list[str] = field(default_factory=list)

    def require_allowed(self) -> None:
        if not self.allowed:
            raise PermissionError("; ".join(self.reasons) or "Tool call rejected by safety policy")


class SafetyPolicy:
    """Small allowlist policy for the local bench prototype."""

    ALWAYS_SAFE_TOOLS = {
        "load_board_context",
        "instrument_status",
        "model_status",
        "safety_status",
        "validate_session",
        "read_audit_log",
        "list_nets",
        "trace_net_neighbors",
        "extract_signal_features",
        "diagnose_hardware",
        "suggest_next_probe",
    }

    SIDE_EFFECT_TOOLS = {"set_power_rail", "capture_waveform", "esp32_set_mux", "esp32_reset_dut"}

    def evaluate(self, tool_name: str, arguments: dict[str, Any], board: BoardContext | None) -> SafetyDecision:
        confirm = bool(arguments.get("confirm", False))
        reasons: list[str] = []
        risk = "low"

        if tool_name in self.ALWAYS_SAFE_TOOLS:
            return SafetyDecision(True, "low", False, [])

        if tool_name not in self.SIDE_EFFECT_TOOLS:
            return SafetyDecision(False, "high", False, [f"Unknown or non-allowlisted tool: {tool_name}"])

        if tool_name == "set_power_rail":
            dry_run = bool(arguments.get("dry_run", True))
            output = bool(arguments.get("output", True))
            if not dry_run and output:
                reasons.append("Non-dry-run power output changes require confirmation")
                risk = _max_risk(risk, "medium")
            rail = arguments.get("rail")
            if board and isinstance(rail, str) and rail in board.rails:
                output_net = board.rails[rail].get("output_net")
                risk = _max_risk(risk, _net_risk(board, output_net))
        elif tool_name == "capture_waveform":
            if board:
                net_name = _argument_net(arguments, board)
                risk = _max_risk(risk, _net_risk(board, net_name))
                point_id = arguments.get("test_point")
                if isinstance(point_id, str) and point_id in board.test_points:
                    risk = _max_risk(risk, board.test_points[point_id].get("risk_level", "low"))
                if _requires_manual_confirmation(board, net_name):
                    reasons.append(f"{net_name} is covered by a manual_confirmation constraint")
            if risk == "high":
                reasons.append("High-risk waveform capture requires confirmation")
        elif tool_name in {"esp32_set_mux", "esp32_reset_dut"}:
            if not bool(arguments.get("dry_run", True)):
                reasons.append(f"Non-dry-run {tool_name} requires confirmation")
                risk = _max_risk(risk, "medium")

        requires_confirmation = bool(reasons)
        if requires_confirmation and not confirm:
            return SafetyDecision(False, risk, True, reasons)
        return SafetyDecision(True, risk, requires_confirmation, reasons)

    def status(self) -> dict[str, Any]:
        return {
            "allowlisted_tools": sorted(self.ALWAYS_SAFE_TOOLS | self.SIDE_EFFECT_TOOLS),
            "side_effect_tools": sorted(self.SIDE_EFFECT_TOOLS),
            "confirmation_argument": "confirm",
        }


class AuditLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        outcome: str,
        decision: SafetyDecision | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "timestamp": utc_now(),
            "tool": tool_name,
            "arguments": _redact(arguments),
            "outcome": outcome,
        }
        if decision is not None:
            event["safety"] = {
                "allowed": decision.allowed,
                "risk_level": decision.risk_level,
                "requires_confirmation": decision.requires_confirmation,
                "reasons": decision.reasons,
            }
        if error:
            event["error"] = error
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return events[-limit:] if limit else events


def _argument_net(arguments: dict[str, Any], board: BoardContext) -> str | None:
    net = arguments.get("net")
    if isinstance(net, str):
        try:
            return board.canonical_net(net)
        except ValueError:
            return net
    return None


def _net_risk(board: BoardContext, net: str | None) -> str:
    if not net or net not in board.nets:
        return "low"
    return board.nets[net].get("risk_level", "low")


def _requires_manual_confirmation(board: BoardContext, net: str | None) -> bool:
    if not net:
        return False
    for constraint in board.data.get("constraints", []) or []:
        if constraint.get("type") == "manual_confirmation" and net in constraint.get("targets", []):
            return True
    return False


def _max_risk(left: str, right: str) -> str:
    order = {"low": 0, "medium": 1, "high": 2}
    return left if order.get(left, 0) >= order.get(right, 0) else right


def _redact(arguments: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in arguments.items():
        if key.lower() in {"password", "token", "api_key", "secret"}:
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted

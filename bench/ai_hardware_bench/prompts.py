"""Prompt templates exposed by the bench MCP server."""

from __future__ import annotations

import json
from typing import Any

from .bench import BenchApp, _nullable_string, _schema, _validate_object_schema


PROMPT_DEFINITIONS = [
    {
        "name": "diagnose_power_rail",
        "description": "Diagnose a power rail from board context and session measurements.",
        "arguments": [
            {"name": "rail", "description": "Rail name or output net to focus on.", "required": False},
        ],
        "inputSchema": _schema({"rail": _nullable_string("Rail name or output net to focus on.")}),
    },
    {
        "name": "diagnose_boot_sequence",
        "description": "Reason about enable, reset, power-good and rail sequencing.",
        "arguments": [
            {"name": "rail", "description": "Optional rail name to anchor the sequence.", "required": False},
        ],
        "inputSchema": _schema({"rail": _nullable_string("Optional rail name to anchor the sequence.")}),
    },
    {
        "name": "plan_next_measurement",
        "description": "Choose the next low-risk measurement from current evidence.",
        "arguments": [
            {"name": "focus_net", "description": "Optional net that should guide the next probe.", "required": False},
        ],
        "inputSchema": _schema({"focus_net": _nullable_string("Optional net that should guide the next probe.")}),
    },
]


def list_prompts() -> list[dict[str, Any]]:
    return PROMPT_DEFINITIONS


def get_prompt(app: BenchApp, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    args = arguments or {}
    if not isinstance(args, dict):
        raise ValueError("prompt arguments must be an object")
    definition = _prompt_definition(name)
    _validate_object_schema(name, args, definition["inputSchema"])
    board = app.require_board()
    session = app.require_session()
    if name == "diagnose_power_rail":
        text = _diagnose_power_rail(app, args.get("rail"))
    elif name == "diagnose_boot_sequence":
        text = _diagnose_boot_sequence(app, args.get("rail"))
    elif name == "plan_next_measurement":
        text = _plan_next_measurement(app, args.get("focus_net"))
    else:
        raise ValueError(f"Unknown prompt: {name}")
    return {
        "description": definition["description"],
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": text,
                },
            }
        ],
        "metadata": {
            "board_id": board.board_id,
            "session_id": session.session_id,
        },
    }


def _prompt_definition(name: str) -> dict[str, Any]:
    for definition in PROMPT_DEFINITIONS:
        if definition["name"] == name:
            return definition
    raise ValueError(f"Unknown prompt: {name}")


def _diagnose_power_rail(app: BenchApp, rail: Any) -> str:
    board = app.require_board()
    session = app.require_session()
    rail_name = str(rail) if rail else _first_rail_name(board)
    rail_info = board.rails.get(rail_name)
    if rail_info is None:
        for candidate in board.rails.values():
            if candidate.get("output_net") == rail_name:
                rail_info = candidate
                rail_name = candidate["name"]
                break
    return "\n".join(
        [
            "You are diagnosing a hardware power rail. Use only the provided structured context.",
            "Return JSON with diagnosis, confidence, evidence, next_measurements and stop_reason.",
            f"Board: {board.data['board']['name']} ({board.board_id})",
            f"Observed symptom: {session.data.get('observed_symptom')}",
            f"Focus rail: {rail_name}",
            f"Rail context: {json.dumps(rail_info or {}, ensure_ascii=False)}",
            f"Relevant measurements: {json.dumps(_measurement_summary(session.data), ensure_ascii=False)}",
            "Respect safety constraints and avoid suggesting direct drive of measurement-only or high-risk nets.",
        ]
    )


def _diagnose_boot_sequence(app: BenchApp, rail: Any) -> str:
    board = app.require_board()
    session = app.require_session()
    rails = list(board.rails.values())
    if rail:
        rails = [item for item in rails if item.get("name") == rail or item.get("output_net") == rail] or rails
    sequence = sorted(rails, key=lambda item: item.get("startup_order", 999))
    return "\n".join(
        [
            "Analyze the DUT boot sequence using rails, enable nets, power-good nets and reset observations.",
            "Return JSON with sequence_assessment, missing_evidence, likely_faults and next_measurements.",
            f"Board: {board.data['board']['name']} ({board.board_id})",
            f"Observed symptom: {session.data.get('observed_symptom')}",
            f"Startup rails: {json.dumps(sequence, ensure_ascii=False)}",
            f"Digital/test points: {json.dumps(list(board.test_points.values()), ensure_ascii=False)}",
            f"Measurements: {json.dumps(_measurement_summary(session.data), ensure_ascii=False)}",
        ]
    )


def _plan_next_measurement(app: BenchApp, focus_net: Any) -> str:
    board = app.require_board()
    session = app.require_session()
    net_name = str(focus_net) if focus_net else None
    trace = None
    if net_name:
        try:
            trace = app.require_topology().trace_net_neighbors(net_name, depth=1)
        except Exception:
            trace = {"error": f"Unknown focus net {net_name}"}
    return "\n".join(
        [
            "Plan the next hardware measurement. Prefer the lowest-risk action that can add new evidence.",
            "Return JSON with next_measurements, risk_notes, required_confirmations and stop_reason.",
            f"Board: {board.data['board']['name']} ({board.board_id})",
            f"Observed symptom: {session.data.get('observed_symptom')}",
            f"Focus net: {net_name or 'not specified'}",
            f"Focus topology: {json.dumps(trace, ensure_ascii=False)}",
            f"Known constraints: {json.dumps(board.data.get('constraints', []), ensure_ascii=False)}",
            f"Measurements: {json.dumps(_measurement_summary(session.data), ensure_ascii=False)}",
            f"Existing next actions: {json.dumps(session.data.get('next_actions', []), ensure_ascii=False)}",
        ]
    )


def _measurement_summary(session: dict[str, Any]) -> list[dict[str, Any]]:
    summary = []
    for measurement in session.get("measurements", []) or []:
        summary.append(
            {
                "id": measurement.get("id"),
                "kind": measurement.get("kind"),
                "target": measurement.get("target"),
                "features": measurement.get("features", {}),
                "result": measurement.get("result", {}),
            }
        )
    return summary


def _first_rail_name(board: Any) -> str:
    return next(iter(board.rails), "unknown")

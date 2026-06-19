"""Diagnostic session validation and regression helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .data import SCHEMA_VERSION, BoardContext, load_board_context, load_document


INSTRUMENT_KINDS = {"psu", "oscilloscope", "dmm", "logic_analyzer", "electronic_load", "esp32_fixture", "other"}
MEASUREMENT_KINDS = {"dc_voltage", "current", "waveform", "logic", "impedance", "thermal", "fixture_state"}
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
ARTIFACT_KINDS = {"waveform_csv", "waveform_binary", "logic_csv", "scope_screenshot", "log", "image", "report", "other"}


def validate_session_file(
    path: str | Path,
    check_artifacts: bool = True,
    board: str | Path | BoardContext | None = None,
) -> dict[str, Any]:
    source = Path(path)
    data = load_document(source)
    board_context = _load_board(board) if board is not None else None
    errors = validate_session(data, base_dir=source.parent, check_artifacts=check_artifacts, board=board_context)
    return {"ok": not errors, "path": str(source), "errors": errors}


def validate_session(
    data: dict[str, Any],
    base_dir: str | Path | None = None,
    check_artifacts: bool = True,
    board: BoardContext | None = None,
) -> list[str]:
    errors: list[str] = []
    _require(data, "schema_version", errors)
    _require(data, "session_id", errors)
    _require(data, "board_id", errors)
    _require(data, "started_at", errors)
    _require(data, "observed_symptom", errors)
    _require(data, "measurements", errors)
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Unsupported schema_version: {data.get('schema_version')!r}")
    if board is not None and data.get("board_id") != board.board_id:
        errors.append(f"session board_id {data.get('board_id')} does not match board context {board.board_id}")
    instruments = _list_field(data, "instruments", errors)
    measurements = _list_field(data, "measurements", errors)
    findings = _list_field(data, "findings", errors)
    next_actions = _list_field(data, "next_actions", errors)
    artifacts = _list_field(data, "artifacts", errors)

    instrument_ids: set[str] = set()
    for index, instrument in enumerate(instruments):
        if not isinstance(instrument, dict):
            errors.append(f"instrument[{index}] must be an object")
            continue
        for field in ("id", "kind"):
            _require(instrument, field, errors, f"instrument[{index}]")
        instrument_id = instrument.get("id")
        if isinstance(instrument_id, str):
            if instrument_id in instrument_ids:
                errors.append(f"Duplicate instrument id: {instrument_id}")
            instrument_ids.add(instrument_id)
        kind = instrument.get("kind")
        if kind is not None and kind not in INSTRUMENT_KINDS:
            errors.append(f"instrument[{index}].kind is not supported: {kind}")
        channels = instrument.get("channels")
        if channels is not None and (
            not isinstance(channels, list) or any(not isinstance(channel, str) for channel in channels)
        ):
            errors.append(f"instrument[{index}].channels must be a list of strings")

    artifact_errors, artifact_ids = _validate_artifacts(artifacts, Path(base_dir) if base_dir else None, check_artifacts)
    errors.extend(artifact_errors)

    measurement_ids: set[str] = set()
    for index, measurement in enumerate(measurements):
        if not isinstance(measurement, dict):
            errors.append(f"measurement[{index}] must be an object")
            continue
        for field in ("id", "timestamp", "kind", "target"):
            _require(measurement, field, errors, f"measurement[{index}]")
        mid = measurement.get("id")
        if isinstance(mid, str):
            if mid in measurement_ids:
                errors.append(f"Duplicate measurement id: {mid}")
            measurement_ids.add(mid)
        kind = measurement.get("kind")
        if kind is not None and kind not in MEASUREMENT_KINDS:
            errors.append(f"measurement[{index}].kind is not supported: {kind}")
        instrument_id = measurement.get("instrument_id")
        if instrument_id and instrument_ids and instrument_id not in instrument_ids:
            errors.append(f"measurement[{index}].instrument_id references unknown instrument {instrument_id}")
        target = measurement.get("target")
        if not isinstance(target, dict) or not target.get("net"):
            errors.append(f"measurement[{index}].target.net is required")
        elif board is not None:
            _validate_target_reference(target, board, errors, f"measurement[{index}].target")
        artifact_refs = measurement.get("artifact_ids", []) or []
        if not isinstance(artifact_refs, list) or any(not isinstance(artifact_id, str) for artifact_id in artifact_refs):
            errors.append(f"measurement[{index}].artifact_ids must be a list of strings")
            artifact_refs = []
        for artifact_id in artifact_refs:
            if artifact_id not in artifact_ids:
                errors.append(f"measurement {mid} references missing artifact {artifact_id}")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"finding[{index}] must be an object")
            continue
        for field in ("id", "timestamp", "summary", "confidence"):
            _require(finding, field, errors, f"finding[{index}]")
        confidence = finding.get("confidence")
        if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
            errors.append(f"finding[{index}].confidence must be between 0 and 1")
        severity = finding.get("severity")
        if severity is not None and severity not in FINDING_SEVERITIES:
            errors.append(f"finding[{index}].severity is not supported: {severity}")
        if board is not None:
            _validate_board_string_refs(
                finding,
                board,
                errors,
                f"finding[{index}]",
                net_field="related_nets",
                component_field="related_components",
            )
    for index, action in enumerate(next_actions):
        if not isinstance(action, dict):
            errors.append(f"next_actions[{index}] must be an object")
            continue
        for field in ("type", "reason", "risk_level"):
            _require(action, field, errors, f"next_actions[{index}]")
        action_type = action.get("type")
        if action_type is not None and action_type not in NEXT_ACTION_TYPES:
            errors.append(f"next_actions[{index}].type is not supported: {action_type}")
        risk_level = action.get("risk_level")
        if risk_level is not None and risk_level not in RISK_LEVELS:
            errors.append(f"next_actions[{index}].risk_level is not supported: {risk_level}")
        if "requires_confirmation" in action and not isinstance(action["requires_confirmation"], bool):
            errors.append(f"next_actions[{index}].requires_confirmation must be boolean")
        if board is not None:
            _validate_action_reference(action, board, errors, f"next_actions[{index}]")
    return errors


def _validate_artifacts(
    artifacts: list[Any],
    base_dir: Path | None,
    check_files: bool,
) -> tuple[list[str], set[str]]:
    errors: list[str] = []
    artifact_ids: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            errors.append(f"artifact[{index}] must be an object")
            continue
        for field in ("id", "kind", "uri"):
            _require(artifact, field, errors, f"artifact[{index}]")
        artifact_id = artifact.get("id")
        if isinstance(artifact_id, str):
            if artifact_id in artifact_ids:
                errors.append(f"Duplicate artifact id: {artifact_id}")
            artifact_ids.add(artifact_id)
        kind = artifact.get("kind")
        if kind is not None and kind not in ARTIFACT_KINDS:
            errors.append(f"artifact[{index}].kind is not supported: {kind}")
        expected_hash = artifact.get("sha256")
        if expected_hash is not None and (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(ch not in "0123456789abcdefABCDEF" for ch in expected_hash)
        ):
            errors.append(f"artifact[{index}].sha256 must be a 64-character hex string")
        uri = artifact.get("uri")
        if not isinstance(uri, str):
            continue
        if not check_files:
            continue
        path = Path(uri)
        if not path.is_absolute() and base_dir is not None and not path.exists():
            path = base_dir / path
        if not path.exists():
            errors.append(f"artifact {artifact.get('id')} file does not exist: {uri}")
            continue
        expected_hash = artifact.get("sha256")
        if expected_hash and _sha256_file(path) != expected_hash:
            errors.append(f"artifact {artifact.get('id')} sha256 mismatch")
    return errors, artifact_ids


def _list_field(data: dict[str, Any], field: str, errors: list[str]) -> list[Any]:
    value = data.get(field, [])
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return []
    return value


def _validate_target_reference(target: dict[str, Any], board: BoardContext, errors: list[str], prefix: str) -> str | None:
    canonical_net = _validate_net_reference(target.get("net"), board, errors, f"{prefix}.net")
    component = target.get("component")
    if component is not None:
        if not isinstance(component, str) or not component:
            errors.append(f"{prefix}.component must be a non-empty string")
        elif component not in board.components:
            errors.append(f"{prefix}.component references unknown component {component}")
    pin = target.get("pin")
    if pin is not None and (not isinstance(pin, str) or not pin):
        errors.append(f"{prefix}.pin must be a non-empty string")
    if isinstance(component, str) and component in board.components and isinstance(pin, str) and pin:
        pins = board.components[component].get("pins", []) or []
        matching_pin = next((item for item in pins if item.get("name") == pin), None)
        if matching_pin is None:
            errors.append(f"{prefix}.pin references unknown pin {component}.{pin}")
        elif canonical_net and matching_pin.get("net") != canonical_net:
            errors.append(f"{prefix}.pin {component}.{pin} is not on net {canonical_net}")
    test_point = target.get("test_point")
    if test_point is not None:
        _validate_test_point_reference(test_point, canonical_net, board, errors, f"{prefix}.test_point")
    return canonical_net


def _validate_action_reference(action: dict[str, Any], board: BoardContext, errors: list[str], prefix: str) -> None:
    canonical_net: str | None = None
    if "net" in action:
        canonical_net = _validate_net_reference(action.get("net"), board, errors, f"{prefix}.net")
    component = action.get("component")
    if component is not None:
        if not isinstance(component, str) or not component:
            errors.append(f"{prefix}.component must be a non-empty string")
        elif component not in board.components:
            errors.append(f"{prefix}.component references unknown component {component}")
    pin = action.get("pin")
    if pin is not None and (not isinstance(pin, str) or not pin):
        errors.append(f"{prefix}.pin must be a non-empty string")
    if isinstance(component, str) and component in board.components and isinstance(pin, str) and pin:
        pins = board.components[component].get("pins", []) or []
        matching_pin = next((item for item in pins if item.get("name") == pin), None)
        if matching_pin is None:
            errors.append(f"{prefix}.pin references unknown pin {component}.{pin}")
        elif canonical_net and matching_pin.get("net") != canonical_net:
            errors.append(f"{prefix}.pin {component}.{pin} is not on net {canonical_net}")
    if "test_point" in action:
        _validate_test_point_reference(action.get("test_point"), canonical_net, board, errors, f"{prefix}.test_point")
    if canonical_net and board.nets[canonical_net].get("risk_level") == "high" and action.get("requires_confirmation") is not True:
        errors.append(f"{prefix} targets high-risk net {canonical_net} without requires_confirmation=true")


def _validate_board_string_refs(
    obj: dict[str, Any],
    board: BoardContext,
    errors: list[str],
    prefix: str,
    net_field: str,
    component_field: str,
) -> None:
    net_values = obj.get(net_field, []) or []
    if isinstance(net_values, list):
        for net in net_values:
            if isinstance(net, str):
                _validate_net_reference(net, board, errors, f"{prefix}.{net_field}")
    component_values = obj.get(component_field, []) or []
    if isinstance(component_values, list):
        for component in component_values:
            if isinstance(component, str) and component not in board.components:
                errors.append(f"{prefix}.{component_field} references unknown component {component}")


def _validate_net_reference(value: Any, board: BoardContext, errors: list[str], prefix: str) -> str | None:
    if not isinstance(value, str) or not value:
        errors.append(f"{prefix} must be a non-empty string")
        return None
    try:
        return board.canonical_net(value)
    except ValueError:
        errors.append(f"{prefix} references unknown net {value}")
        return None


def _validate_test_point_reference(
    value: Any,
    canonical_net: str | None,
    board: BoardContext,
    errors: list[str],
    prefix: str,
) -> None:
    if not isinstance(value, str) or not value:
        errors.append(f"{prefix} must be a non-empty string")
    elif value not in board.test_points:
        errors.append(f"{prefix} references unknown test point {value}")
    elif canonical_net and board.test_points[value].get("net") != canonical_net:
        errors.append(f"{prefix} {value} is not on net {canonical_net}")


def _load_board(board: str | Path | BoardContext) -> BoardContext:
    if isinstance(board, BoardContext):
        return board
    return load_board_context(board)


def _require(obj: dict[str, Any], field: str, errors: list[str], prefix: str | None = None) -> None:
    if field not in obj:
        errors.append(f"{prefix + '.' if prefix else ''}{field} is required")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

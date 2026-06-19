"""Diagnostic session validation and regression helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .data import SCHEMA_VERSION, load_document


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


def validate_session_file(path: str | Path, check_artifacts: bool = True) -> dict[str, Any]:
    source = Path(path)
    data = load_document(source)
    errors = validate_session(data, base_dir=source.parent, check_artifacts=check_artifacts)
    return {"ok": not errors, "path": str(source), "errors": errors}


def validate_session(
    data: dict[str, Any],
    base_dir: str | Path | None = None,
    check_artifacts: bool = True,
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


def _require(obj: dict[str, Any], field: str, errors: list[str], prefix: str | None = None) -> None:
    if field not in obj:
        errors.append(f"{prefix + '.' if prefix else ''}{field} is required")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

"""Diagnostic session validation and regression helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .data import SCHEMA_VERSION, load_document


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
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Unsupported schema_version: {data.get('schema_version')!r}")
    measurements = data.get("measurements")
    if not isinstance(measurements, list):
        errors.append("measurements must be a list")
        measurements = []
    artifact_ids = {artifact.get("id") for artifact in data.get("artifacts", []) if isinstance(artifact, dict)}
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
        target = measurement.get("target")
        if not isinstance(target, dict) or not target.get("net"):
            errors.append(f"measurement[{index}].target.net is required")
        for artifact_id in measurement.get("artifact_ids", []) or []:
            if artifact_id not in artifact_ids:
                errors.append(f"measurement {mid} references missing artifact {artifact_id}")
    for index, finding in enumerate(data.get("findings", []) or []):
        if not isinstance(finding, dict):
            errors.append(f"finding[{index}] must be an object")
            continue
        for field in ("id", "timestamp", "summary", "confidence"):
            _require(finding, field, errors, f"finding[{index}]")
        confidence = finding.get("confidence")
        if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
            errors.append(f"finding[{index}].confidence must be between 0 and 1")
    for index, action in enumerate(data.get("next_actions", []) or []):
        if not isinstance(action, dict):
            errors.append(f"next_actions[{index}] must be an object")
            continue
        for field in ("type", "reason", "risk_level"):
            _require(action, field, errors, f"next_actions[{index}]")
    if check_artifacts:
        errors.extend(_validate_artifacts(data.get("artifacts", []) or [], Path(base_dir) if base_dir else None))
    return errors


def _validate_artifacts(artifacts: list[dict[str, Any]], base_dir: Path | None) -> list[str]:
    errors: list[str] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            errors.append(f"artifact[{index}] must be an object")
            continue
        for field in ("id", "kind", "uri"):
            _require(artifact, field, errors, f"artifact[{index}]")
        uri = artifact.get("uri")
        if not isinstance(uri, str):
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
    return errors


def _require(obj: dict[str, Any], field: str, errors: list[str], prefix: str | None = None) -> None:
    if field not in obj:
        errors.append(f"{prefix + '.' if prefix else ''}{field} is required")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


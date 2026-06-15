"""Regression task runner for repeatable diagnostic workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bench import BenchApp
from .data import load_document
from .session import validate_session_file


DEFAULT_TASK = {
    "id": "usb_power_stage_vout_collapse",
    "board": "examples/boards/usb_power_stage.yaml",
    "symptom": "3V3 rail does not stay up after USB input is applied.",
    "expected": {
        "severity": ["warning", "fault"],
        "next_action_net": "SW_NODE",
    },
}


def run_regression_suite(
    suite_path: str | Path | None = None,
    artifact_dir: str | Path = "artifacts/regression",
) -> dict[str, Any]:
    tasks = [DEFAULT_TASK] if suite_path is None else _load_tasks(suite_path)
    root = Path(artifact_dir)
    results = []
    for task in tasks:
        results.append(run_regression_task(task, root / task["id"]))
    passed = sum(1 for item in results if item["ok"])
    return {
        "ok": passed == len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "count": len(results),
        "results": results,
    }


def run_regression_task(task: dict[str, Any], artifact_dir: Path) -> dict[str, Any]:
    output_session = artifact_dir / "session.json"
    app = BenchApp(artifact_dir)
    result = app.demo(task["board"], task["symptom"], output_session)
    validation = validate_session_file(output_session)
    finding = result["diagnosis"]["finding"]
    actions = result["diagnosis"].get("next_actions", [])
    checks = _check_expected(task.get("expected", {}), finding, actions)
    ok = validation["ok"] and not checks
    return {
        "ok": ok,
        "id": task["id"],
        "board": task["board"],
        "session_path": str(output_session),
        "finding_summary": finding["summary"],
        "finding_severity": finding["severity"],
        "next_actions": actions,
        "validation_errors": validation["errors"],
        "check_errors": checks,
    }


def _load_tasks(path: str | Path) -> list[dict[str, Any]]:
    loaded = load_document(path)
    raw_tasks = loaded.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError(f"Regression suite {path} must contain a non-empty tasks list")
    tasks: list[dict[str, Any]] = []
    for index, task in enumerate(raw_tasks):
        if not isinstance(task, dict):
            raise ValueError(f"tasks[{index}] must be an object")
        for field in ("id", "board", "symptom"):
            if field not in task:
                raise ValueError(f"tasks[{index}] is missing {field}")
        tasks.append(task)
    return tasks


def _check_expected(expected: dict[str, Any], finding: dict[str, Any], actions: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    severities = expected.get("severity")
    if isinstance(severities, str):
        severities = [severities]
    if severities and finding.get("severity") not in severities:
        errors.append(f"severity {finding.get('severity')} not in expected {severities}")
    summary_contains = expected.get("summary_contains")
    if summary_contains and summary_contains not in finding.get("summary", ""):
        errors.append(f"summary does not contain {summary_contains!r}")
    next_action_net = expected.get("next_action_net")
    if next_action_net and not any(action.get("net") == next_action_net for action in actions):
        errors.append(f"no next action targets net {next_action_net}")
    return errors


def write_regression_result(result: dict[str, Any], output: str | Path) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


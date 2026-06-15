"""Repository quality gate for no-hardware validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


@dataclass
class GateStep:
    name: str
    command: list[str]


def run_quality_gate(
    root: str | Path = ".",
    artifact_dir: str | Path = "artifacts/check",
    skip_esp32_bundle: bool = False,
) -> dict[str, Any]:
    repo = Path(root).resolve()
    artifacts = Path(artifact_dir)
    if not artifacts.is_absolute():
        artifacts = repo / artifacts
    steps = [
        GateStep("validate_board", [sys.executable, "tools/bench.py", "validate-board", "examples/boards/usb_power_stage.yaml"]),
        GateStep("python_compile", _py_compile_command(repo)),
        GateStep("unit_tests", _with_pythonpath(repo, [sys.executable, "-m", "unittest", "discover", "-s", "tests"])),
        GateStep(
            "regression_suite",
            [
                sys.executable,
                "tools/bench.py",
                "run-regression",
                "--suite",
                "examples/regressions/usb_power_stage.json",
                "--artifact-dir",
                str(artifacts / "regression"),
                "--output",
                str(artifacts / "regression" / "result.json"),
            ],
        ),
        GateStep(
            "report_generation",
            [
                sys.executable,
                "tools/bench.py",
                "report",
                "--session",
                str(artifacts / "regression" / "usb_power_stage_vout_collapse" / "session.json"),
                "--output",
                str(artifacts / "regression" / "usb_power_stage_vout_collapse" / "report.html"),
                "--audit",
                str(artifacts / "regression" / "usb_power_stage_vout_collapse" / "audit.jsonl"),
            ],
        ),
    ]
    bundle = repo / "firmware" / "esp32-fixture" / "dist" / "esp32-fixture.zip"
    if not skip_esp32_bundle and bundle.exists():
        steps.append(
            GateStep(
                "esp32_bundle_verify",
                [
                    sys.executable,
                    "firmware/esp32-fixture/tools/deploy.py",
                    "verify-bundle",
                    "--bundle",
                    str(bundle),
                ],
            )
        )
    elif not skip_esp32_bundle:
        steps.append(GateStep("esp32_bundle_missing", [sys.executable, "-c", "import sys; sys.exit(2)"]))

    results = [_run_step(repo, step) for step in steps]
    return {
        "ok": all(result["ok"] for result in results),
        "artifact_dir": str(artifacts),
        "steps": results,
    }


def write_quality_result(result: dict[str, Any], output: str | Path) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def _run_step(repo: Path, step: GateStep) -> dict[str, Any]:
    completed = subprocess.run(
        step.command,
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(repo),
    )
    return {
        "name": step.name,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "command": step.command,
        "stdout": completed.stdout[-6000:],
        "stderr": completed.stderr[-6000:],
    }


def _py_compile_command(repo: Path) -> list[str]:
    excluded_parts = {"artifacts", "__pycache__", ".git", "build", "managed_components"}
    files = [
        str(path.relative_to(repo))
        for path in sorted(repo.rglob("*.py"))
        if not excluded_parts.intersection(path.parts)
    ]
    return [sys.executable, "-m", "py_compile", *files]


def _with_pythonpath(repo: Path, command: list[str]) -> list[str]:
    return command


def _env(repo: Path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    bench = str(repo / "bench")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = bench if not existing else f"{bench}:{existing}"
    return env

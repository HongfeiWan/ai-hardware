"""Command line entry point for the bench prototype."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .bench import BenchApp
from .data import load_board_context
from .importers import import_board
from .mcp_server import main as serve_main
from .quality import run_quality_gate, write_quality_result
from .regression import run_regression_suite, run_regression_task, write_regression_result
from .report import generate_session_report
from .session import validate_session_file
from .web import run_console


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Hardware Python bench prototype")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-board", help="Load and validate a board context file")
    validate.add_argument("path")

    validate_sess = sub.add_parser("validate-session", help="Validate a diagnostic session JSON file")
    validate_sess.add_argument("path")
    validate_sess.add_argument("--no-artifacts", action="store_true", help="Skip artifact existence/hash checks")
    validate_sess.add_argument("--board", help="Optional board context file for net/component/test point reference checks")

    demo = sub.add_parser("demo", help="Run a full mock diagnostic flow")
    demo.add_argument("--board", default="examples/boards/usb_power_stage.yaml")
    demo.add_argument("--symptom", default="3V3 rail does not stay up after USB input is applied.")
    demo.add_argument("--artifact-dir", default="artifacts/mock-bench")
    demo.add_argument("--output-session", default="artifacts/mock-bench/session.json")
    demo.add_argument("--instrument-config", help="Optional JSON config for real instrument drivers")
    demo.add_argument("--model-config", help="Optional JSON config for model adapter")

    regression = sub.add_parser("run-regression", help="Run the built-in mock diagnostic regression")
    regression.add_argument("--board", default="examples/boards/usb_power_stage.yaml")
    regression.add_argument("--symptom", default="3V3 rail does not stay up after USB input is applied.")
    regression.add_argument("--artifact-dir", default="artifacts/regression")
    regression.add_argument("--suite", help="Regression suite JSON/YAML file")
    regression.add_argument("--output", help="Write regression result JSON")

    report = sub.add_parser("report", help="Generate a static HTML diagnostic report")
    report.add_argument("--session", required=True)
    report.add_argument("--output", required=True)
    report.add_argument("--audit", help="Optional audit JSONL file")

    call = sub.add_parser("call-tool", help="Call one bench tool after loading a board")
    call.add_argument("name")
    call.add_argument("--board", default="examples/boards/usb_power_stage.yaml")
    call.add_argument("--symptom", default="unspecified symptom")
    call.add_argument("--arguments", default="{}", help="JSON object passed to the tool")
    call.add_argument("--artifact-dir", default="artifacts/mock-bench")
    call.add_argument("--instrument-config", help="Optional JSON config for real instrument drivers")
    call.add_argument("--model-config", help="Optional JSON config for model adapter")

    importer = sub.add_parser(
        "import-board",
        help="Convert CSV, Altium CSV/TSV, BOM CSV/TSV, pick-and-place CSV/TSV or KiCad netlists to board_context JSON",
    )
    importer.add_argument(
        "--format",
        choices=[
            "csv",
            "altium",
            "altium-csv",
            "altium-tsv",
            "bom",
            "bom-csv",
            "bom-tsv",
            "pnp",
            "pick-place",
            "pick-and-place",
            "pickplace",
            "kicad",
            "kicad-xml",
            "kicad-sexpr",
            "kicad-pcb",
            "kicad-sch",
        ],
        required=True,
    )
    importer.add_argument("--input", required=True)
    importer.add_argument("--output", required=True)
    importer.add_argument("--board-id", required=True)
    importer.add_argument("--name", required=True)

    serve = sub.add_parser("serve", help="Run the minimal MCP stdio server")
    serve.add_argument("--artifact-dir", default="artifacts/mock-bench")
    serve.add_argument("--board")
    serve.add_argument("--symptom", default="unspecified symptom")
    serve.add_argument("--instrument-config", help="Optional JSON config for real instrument drivers")
    serve.add_argument("--model-config", help="Optional JSON config for model adapter")

    console = sub.add_parser("console", help="Run the local HTTP bench console")
    console.add_argument("--host", default="127.0.0.1")
    console.add_argument("--port", type=int, default=8766)
    console.add_argument("--board", default="examples/boards/usb_power_stage.yaml")
    console.add_argument("--artifact-dir", default="artifacts/console")
    console.add_argument("--symptom", default="3V3 rail does not stay up after USB input is applied.")
    console.add_argument("--suite", default="examples/regressions/usb_power_stage.json")

    check = sub.add_parser("check", help="Run the no-hardware repository quality gate")
    check.add_argument("--artifact-dir", default="artifacts/check")
    check.add_argument("--output", help="Write quality gate result JSON")
    check.add_argument("--skip-esp32-bundle", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "validate-board":
        board = load_board_context(args.path)
        print(
            json.dumps(
                {
                    "ok": True,
                    "board_id": board.board_id,
                    "nets": len(board.nets),
                    "components": len(board.components),
                    "test_points": len(board.test_points),
                    "rails": len(board.rails),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "validate-session":
        result = validate_session_file(args.path, check_artifacts=not args.no_artifacts, board=args.board)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 1
    if args.command == "demo":
        app = BenchApp(args.artifact_dir, instrument_config=args.instrument_config, model_config=args.model_config)
        result = app.demo(args.board, args.symptom, args.output_session)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.command == "run-regression":
        if args.suite:
            result = run_regression_suite(args.suite, args.artifact_dir)
        else:
            task = {
                "id": "single_regression",
                "board": args.board,
                "symptom": args.symptom,
                "expected": {"severity": ["info", "warning", "fault"]},
            }
            single = run_regression_task(task, Path(args.artifact_dir) / task["id"])
            result = {
                "ok": single["ok"],
                "passed": 1 if single["ok"] else 0,
                "failed": 0 if single["ok"] else 1,
                "count": 1,
                "results": [single],
            }
        if args.output:
            write_regression_result(result, args.output)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 1
    if args.command == "report":
        result = generate_session_report(args.session, args.output, audit_path=args.audit)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.command == "call-tool":
        app = BenchApp(args.artifact_dir, instrument_config=args.instrument_config, model_config=args.model_config)
        app.load_board_context_tool(args.board, observed_symptom=args.symptom)
        try:
            arguments = json.loads(args.arguments)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--arguments must be JSON: {exc}") from exc
        if not isinstance(arguments, dict):
            raise SystemExit("--arguments must decode to a JSON object")
        result = app.call_tool(args.name, arguments)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.command == "import-board":
        result = import_board(args.input, args.format, args.board_id, args.name, args.output)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.command == "serve":
        server_args = ["--artifact-dir", args.artifact_dir]
        if args.instrument_config:
            server_args.extend(["--instrument-config", args.instrument_config])
        if args.model_config:
            server_args.extend(["--model-config", args.model_config])
        if args.board:
            server_args.extend(["--board", args.board, "--symptom", args.symptom])
        return serve_main(server_args)
    if args.command == "console":
        run_console(args.host, args.port, args.board, args.artifact_dir, args.symptom, args.suite)
        return 0
    if args.command == "check":
        result = run_quality_gate(".", args.artifact_dir, skip_esp32_bundle=args.skip_esp32_bundle)
        if args.output:
            write_quality_result(result, args.output)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 1
    raise AssertionError(f"Unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Command line entry point for the bench prototype."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .bench import BenchApp
from .data import load_board_context
from .mcp_server import main as serve_main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Hardware Python bench prototype")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-board", help="Load and validate a board context file")
    validate.add_argument("path")

    demo = sub.add_parser("demo", help="Run a full mock diagnostic flow")
    demo.add_argument("--board", default="examples/boards/usb_power_stage.yaml")
    demo.add_argument("--symptom", default="3V3 rail does not stay up after USB input is applied.")
    demo.add_argument("--artifact-dir", default="artifacts/mock-bench")
    demo.add_argument("--output-session", default="artifacts/mock-bench/session.json")

    call = sub.add_parser("call-tool", help="Call one bench tool after loading a board")
    call.add_argument("name")
    call.add_argument("--board", default="examples/boards/usb_power_stage.yaml")
    call.add_argument("--symptom", default="unspecified symptom")
    call.add_argument("--arguments", default="{}", help="JSON object passed to the tool")
    call.add_argument("--artifact-dir", default="artifacts/mock-bench")

    serve = sub.add_parser("serve", help="Run the minimal MCP stdio server")
    serve.add_argument("--artifact-dir", default="artifacts/mock-bench")
    serve.add_argument("--board")
    serve.add_argument("--symptom", default="unspecified symptom")

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
    if args.command == "demo":
        app = BenchApp(args.artifact_dir)
        result = app.demo(args.board, args.symptom, args.output_session)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.command == "call-tool":
        app = BenchApp(args.artifact_dir)
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
    if args.command == "serve":
        server_args = ["--artifact-dir", args.artifact_dir]
        if args.board:
            server_args.extend(["--board", args.board, "--symptom", args.symptom])
        return serve_main(server_args)
    raise AssertionError(f"Unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


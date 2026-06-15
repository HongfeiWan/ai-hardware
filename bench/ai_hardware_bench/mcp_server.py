"""Minimal MCP-compatible stdio server for the bench prototype."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .bench import BenchApp, to_mcp_tool_result
from .prompts import get_prompt, list_prompts


class StdioJsonRpcServer:
    def __init__(self, app: BenchApp) -> None:
        self.app = app

    def serve_forever(self) -> None:
        while True:
            message = self._read_message()
            if message is None:
                break
            response = self.handle(message)
            if response is not None:
                self._write_message(response)

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if request_id is None:
            return None
        try:
            result = self._dispatch(method, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": str(exc),
                },
            }

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": "ai-hardware-bench", "version": "0.1.0"},
            }
        if method == "tools/list":
            return {"tools": self.app.list_tools()}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise ValueError("tools/call arguments must be an object")
            return to_mcp_tool_result(self.app.call_tool(name, arguments))
        if method == "resources/list":
            board = self.app.board
            session = self.app.session
            resources = []
            if board:
                resources.extend(
                    [
                        {
                            "uri": f"board://context/{board.board_id}",
                            "name": "Board context",
                            "mimeType": "application/json",
                        },
                        {
                            "uri": f"board://topology/{board.board_id}",
                            "name": "Board topology",
                            "mimeType": "application/json",
                        },
                    ]
                )
            if session:
                resources.append(
                    {
                        "uri": f"session://measurements/{session.session_id}",
                        "name": "Diagnostic session",
                        "mimeType": "application/json",
                    }
                )
            return {"resources": resources}
        if method == "resources/read":
            uri = params.get("uri")
            payload = self.app.read_resource(uri)
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(payload["content"] if "content" in payload else payload, ensure_ascii=False, indent=2),
                    }
                ]
            }
        if method == "prompts/list":
            return {"prompts": list_prompts()}
        if method == "prompts/get":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str):
                raise ValueError("prompts/get name must be a string")
            if not isinstance(arguments, dict):
                raise ValueError("prompts/get arguments must be an object")
            return get_prompt(self.app, name, arguments)
        raise ValueError(f"Unsupported JSON-RPC method: {method}")

    def _read_message(self) -> dict[str, Any] | None:
        first = sys.stdin.buffer.readline()
        if not first:
            return None
        if first.startswith(b"Content-Length:"):
            length = int(first.split(b":", 1)[1].strip())
            while True:
                line = sys.stdin.buffer.readline()
                if line in {b"\r\n", b"\n", b""}:
                    break
            body = sys.stdin.buffer.read(length)
            return json.loads(body.decode("utf-8"))
        return json.loads(first.decode("utf-8"))

    def _write_message(self, message: dict[str, Any]) -> None:
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AI Hardware bench MCP stdio server.")
    parser.add_argument("--artifact-dir", default="artifacts/mock-bench")
    parser.add_argument("--instrument-config")
    parser.add_argument("--model-config")
    parser.add_argument("--board")
    parser.add_argument("--symptom", default="unspecified symptom")
    args = parser.parse_args(argv)
    app = BenchApp(args.artifact_dir, instrument_config=args.instrument_config, model_config=args.model_config)
    if args.board:
        app.load_board_context_tool(args.board, observed_symptom=args.symptom)
    StdioJsonRpcServer(app).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Dependency-free local web console for bench diagnostics."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .bench import BenchApp
from .data import load_board_context
from .regression import run_regression_suite
from .report import generate_session_report


class ConsoleState:
    def __init__(
        self,
        board_path: str | Path,
        artifact_dir: str | Path,
        symptom: str,
        suite_path: str | Path | None = None,
    ) -> None:
        self.board_path = str(board_path)
        self.artifact_dir = Path(artifact_dir)
        self.symptom = symptom
        self.suite_path = str(suite_path) if suite_path else None
        self.last_demo: dict[str, Any] | None = None
        self.last_regression: dict[str, Any] | None = None


def create_console_server(
    host: str = "127.0.0.1",
    port: int = 8766,
    board_path: str | Path = "examples/boards/usb_power_stage.yaml",
    artifact_dir: str | Path = "artifacts/console",
    symptom: str = "3V3 rail does not stay up after USB input is applied.",
    suite_path: str | Path | None = "examples/regressions/usb_power_stage.json",
) -> ThreadingHTTPServer:
    state = ConsoleState(board_path, artifact_dir, symptom, suite_path)

    class Handler(ConsoleHandler):
        console_state = state

    return ThreadingHTTPServer((host, port), Handler)


class ConsoleHandler(BaseHTTPRequestHandler):
    console_state: ConsoleState
    server_version = "AIHardwareBenchConsole/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(render_console_html(self.console_state))
            return
        if parsed.path == "/api/status":
            self._send_json(self._status_payload())
            return
        if parsed.path == "/api/board":
            self._send_json(self._board_payload())
            return
        if parsed.path.startswith("/reports/"):
            self._serve_artifact_file(parsed.path.removeprefix("/reports/"))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/demo":
            payload = self._read_json_body()
            symptom = str(payload.get("symptom") or self.console_state.symptom)
            result = self._run_demo(symptom)
            self._send_json(result)
            return
        if parsed.path == "/api/regression":
            result = run_regression_suite(self.console_state.suite_path, self.console_state.artifact_dir / "regression")
            self.console_state.last_regression = result
            self._send_json(result)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _status_payload(self) -> dict[str, Any]:
        app = BenchApp(self.console_state.artifact_dir / "status")
        board = load_board_context(self.console_state.board_path)
        return {
            "ok": True,
            "board_path": self.console_state.board_path,
            "artifact_dir": str(self.console_state.artifact_dir),
            "suite_path": self.console_state.suite_path,
            "board": {
                "id": board.board_id,
                "name": board.data["board"]["name"],
                "nets": len(board.nets),
                "components": len(board.components),
                "test_points": len(board.test_points),
                "rails": len(board.rails),
            },
            "instruments": app.instrument_status()["instruments"],
            "model": app.model_status()["model"],
            "last_demo": _compact_last(self.console_state.last_demo),
            "last_regression": _compact_last(self.console_state.last_regression),
        }

    def _board_payload(self) -> dict[str, Any]:
        board = load_board_context(self.console_state.board_path)
        return {
            "ok": True,
            "board": board.data["board"],
            "nets": list(board.nets.values()),
            "rails": list(board.rails.values()),
            "test_points": list(board.test_points.values()),
        }

    def _run_demo(self, symptom: str) -> dict[str, Any]:
        demo_dir = self.console_state.artifact_dir / "demo"
        session_path = demo_dir / "session.json"
        report_path = demo_dir / "report.html"
        app = BenchApp(demo_dir)
        result = app.demo(self.console_state.board_path, symptom, session_path)
        report = generate_session_report(session_path, report_path, audit_path=demo_dir / "audit.jsonl")
        payload = {
            "ok": True,
            "demo": result,
            "report": report,
            "report_url": "/reports/demo/report.html",
        }
        self.console_state.last_demo = payload
        return payload

    def _serve_artifact_file(self, raw_relative: str) -> None:
        relative = Path(unquote(raw_relative))
        if relative.is_absolute() or ".." in relative.parts:
            self.send_error(HTTPStatus.FORBIDDEN, "Invalid report path")
            return
        target = (self.console_state.artifact_dir / relative).resolve()
        root = self.console_state.artifact_dir.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "Invalid report path")
            return
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Report not found")
            return
        content_type = "text/html; charset=utf-8" if target.suffix == ".html" else "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
            return {}
        if not isinstance(payload, dict):
            self.send_error(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
            return {}
        return payload

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_console(
    host: str,
    port: int,
    board_path: str | Path,
    artifact_dir: str | Path,
    symptom: str,
    suite_path: str | Path | None,
) -> None:
    server = create_console_server(host, port, board_path, artifact_dir, symptom, suite_path)
    print(f"AI Hardware bench console listening on http://{host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def render_console_html(state: ConsoleState) -> str:
    title = "AI Hardware Bench Console"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #5c6673;
      --line: #d9dfe7;
      --accent: #0f766e;
      --accent-weak: #e6f4f1;
      --warn: #b45309;
      --bad: #b91c1c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      padding: 18px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}
    h1 {{ margin: 0; font-size: 22px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 10px; font-size: 16px; letter-spacing: 0; }}
    main {{ padding: 18px 24px 32px; display: grid; gap: 16px; }}
    section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }}
    .metric {{ border: 1px solid var(--line); border-radius: 6px; padding: 12px; background: #fbfcfd; }}
    .metric span {{ color: var(--muted); display: block; }}
    .metric strong {{ display: block; margin-top: 5px; font-size: 20px; }}
    button, a.button {{
      appearance: none;
      border: 1px solid #0b615a;
      background: var(--accent);
      color: white;
      border-radius: 6px;
      padding: 8px 12px;
      font: inherit;
      text-decoration: none;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      min-height: 36px;
    }}
    button.secondary, a.secondary {{ background: #fff; color: var(--accent); border-color: var(--line); }}
    textarea {{
      width: 100%;
      min-height: 72px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px;
      font: inherit;
      resize: vertical;
    }}
    code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #101820;
      color: #eef6f6;
      border-radius: 6px;
      padding: 12px;
      max-height: 360px;
      overflow: auto;
    }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
    .muted {{ color: var(--muted); }}
    .ok {{ color: var(--accent); font-weight: 700; }}
    .bad {{ color: var(--bad); font-weight: 700; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>AI Hardware Bench Console</h1>
      <div class="muted"><code>{escape(state.board_path)}</code></div>
    </div>
    <div class="toolbar">
      <button id="refresh">Refresh</button>
      <button id="run-demo">Run Demo</button>
      <button id="run-regression" class="secondary">Run Regression</button>
    </div>
  </header>
  <main>
    <section>
      <h2>Status</h2>
      <div id="status" class="grid"></div>
    </section>
    <section>
      <h2>Symptom</h2>
      <textarea id="symptom">{escape(state.symptom)}</textarea>
    </section>
    <section>
      <h2>Result</h2>
      <div id="links" class="toolbar"></div>
      <pre id="output">Ready.</pre>
    </section>
  </main>
  <script>
    const statusEl = document.getElementById('status');
    const outputEl = document.getElementById('output');
    const linksEl = document.getElementById('links');
    const symptomEl = document.getElementById('symptom');

    function show(data) {{
      outputEl.textContent = JSON.stringify(data, null, 2);
    }}

    function setLinks(data) {{
      linksEl.innerHTML = '';
      if (data.report_url) {{
        const link = document.createElement('a');
        link.className = 'button secondary';
        link.href = data.report_url;
        link.textContent = 'Open Report';
        linksEl.appendChild(link);
      }}
    }}

    async function refresh() {{
      const response = await fetch('/api/status');
      const data = await response.json();
      statusEl.innerHTML = [
        ['Board', data.board.id],
        ['Nets', data.board.nets],
        ['Components', data.board.components],
        ['Test Points', data.board.test_points],
        ['Rails', data.board.rails],
        ['Model', data.model.backend]
      ].map(([label, value]) => `<div class="metric"><span>${{label}}</span><strong>${{value}}</strong></div>`).join('');
      show(data);
    }}

    async function runDemo() {{
      const response = await fetch('/api/demo', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ symptom: symptomEl.value }})
      }});
      const data = await response.json();
      setLinks(data);
      show(data);
      await refresh();
    }}

    async function runRegression() {{
      const response = await fetch('/api/regression', {{ method: 'POST' }});
      const data = await response.json();
      setLinks({{}});
      show(data);
      await refresh();
    }}

    document.getElementById('refresh').addEventListener('click', refresh);
    document.getElementById('run-demo').addEventListener('click', runDemo);
    document.getElementById('run-regression').addEventListener('click', runRegression);
    refresh();
  </script>
</body>
</html>
"""


def _compact_last(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    if "report_url" in payload:
        return {"ok": payload.get("ok"), "report_url": payload.get("report_url")}
    return {
        "ok": payload.get("ok"),
        "passed": payload.get("passed"),
        "failed": payload.get("failed"),
        "count": payload.get("count"),
    }


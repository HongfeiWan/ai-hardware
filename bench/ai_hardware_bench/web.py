"""Dependency-free local web console for bench diagnostics."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .bench import BenchApp
from .data import load_board_context, load_document
from .importers import import_board
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
        self.last_plan: dict[str, Any] | None = None
        self.last_import: dict[str, Any] | None = None


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
        if parsed.path == "/api/replay":
            query = parse_qs(parsed.query)
            max_points = int((query.get("max_points") or ["160"])[0])
            self._send_json(self._replay_payload(max_points=max_points))
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
        if parsed.path == "/api/plan":
            payload = self._read_json_body()
            result = self._plan_initial_measurements(payload)
            self.console_state.last_plan = result
            self._send_json(result)
            return
        if parsed.path == "/api/import-board":
            payload = self._read_json_body()
            result = self._import_board(payload)
            self.console_state.last_import = result
            self._send_json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
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
            "last_import": _compact_last(self.console_state.last_import),
            "last_plan": _compact_last(self.console_state.last_plan),
            "last_demo": _compact_last(self.console_state.last_demo),
            "last_regression": _compact_last(self.console_state.last_regression),
        }

    def _board_payload(self) -> dict[str, Any]:
        app = BenchApp(self.console_state.artifact_dir / "board")
        app.load_board_context_tool(self.console_state.board_path, observed_symptom=self.console_state.symptom)
        board = app.require_board()
        return {
            "ok": True,
            "board": board.data["board"],
            "nets": app.list_nets()["nets"],
            "rails": list(board.rails.values()),
            "test_points": list(board.test_points.values()),
            "power_paths": [
                app.trace_power_path(rail["output_net"])
                for rail in sorted(board.rails.values(), key=lambda item: (item.get("startup_order", 999), item["name"]))
            ],
        }

    def _replay_payload(self, max_points: int = 160) -> dict[str, Any]:
        session_path = self.console_state.artifact_dir / "demo" / "session.json"
        if not session_path.exists():
            return {
                "ok": False,
                "reason": "No demo session is available yet.",
                "session_path": str(session_path),
                "waveforms": [],
            }
        session = load_document(session_path)
        artifacts = session.get("artifacts", []) or []
        waveforms = []
        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get("kind") != "waveform_csv":
                continue
            path = _resolve_console_artifact_path(artifact.get("uri"), self.console_state.artifact_dir, session_path.parent)
            if path is None:
                waveforms.append({"artifact": artifact, "ok": False, "reason": "Artifact file is missing."})
                continue
            try:
                samples = _read_waveform_csv(path)
            except Exception as exc:
                waveforms.append({"artifact": artifact, "ok": False, "reason": str(exc)})
                continue
            measurement = _measurement_for_artifact(session, str(artifact.get("id", "")))
            waveforms.append(
                {
                    "ok": True,
                    "artifact": artifact,
                    "measurement": measurement,
                    "sample_count": len(samples),
                    "samples": _decimate_samples(samples, max_points=max(16, min(max_points, 1000))),
                }
            )
        return {
            "ok": True,
            "session_id": session.get("session_id"),
            "session_path": str(session_path),
            "measurement_count": len(session.get("measurements", []) or []),
            "finding_count": len(session.get("findings", []) or []),
            "waveforms": waveforms,
        }

    def _plan_initial_measurements(self, payload: dict[str, Any]) -> dict[str, Any]:
        app = BenchApp(self.console_state.artifact_dir / "plan")
        app.load_board_context_tool(self.console_state.board_path, observed_symptom=self.console_state.symptom)
        arguments = {
            "max_actions": int(payload.get("max_actions", 8)),
            "risk_ceiling": str(payload.get("risk_ceiling", "medium")),
            "include_power_off": bool(payload.get("include_power_off", True)),
        }
        return app.call_tool("plan_initial_measurements", arguments)

    def _import_board(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_format = str(payload.get("format") or "csv")
        if source_format not in {"csv", "kicad", "kicad-xml"}:
            return {"ok": False, "error": f"Unsupported import format: {source_format}"}
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            return {"ok": False, "error": "Import content is required"}
        board_id = _safe_identifier(str(payload.get("board_id") or "imported_board"))
        board_name = str(payload.get("name") or board_id.replace("_", " ").title())
        suffix = ".csv" if source_format == "csv" else ".xml"
        import_dir = self.console_state.artifact_dir / "imports" / board_id
        source_path = import_dir / f"source{suffix}"
        output_path = import_dir / "board_context.json"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(content, encoding="utf-8")
        try:
            result = import_board(source_path, source_format, board_id, board_name, output_path)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "source_path": str(source_path)}
        self.console_state.board_path = str(output_path)
        self.console_state.last_demo = None
        self.console_state.last_plan = None
        return {
            "ok": True,
            "format": source_format,
            "source_path": str(source_path),
            "board_path": str(output_path),
            "import": result,
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
    input, select, textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px;
      font: inherit;
      background: #fff;
    }}
    textarea {{
      min-height: 72px;
      resize: vertical;
    }}
    label {{ display: grid; gap: 5px; color: var(--muted); }}
    .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin-bottom: 10px; }}
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
    .split {{ display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(320px, .9fr); gap: 16px; align-items: start; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 620px; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ background: #eef3f2; color: #22313a; font-weight: 700; }}
    tr:last-child td {{ border-bottom: 0; }}
    .pill {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 2px 8px; background: var(--accent-weak); color: #07564f; font-size: 12px; }}
    .pill.warn {{ background: #fff7ed; color: var(--warn); }}
    .pill.bad {{ background: #fee2e2; color: var(--bad); }}
    .muted {{ color: var(--muted); }}
    .ok {{ color: var(--accent); font-weight: 700; }}
    .bad {{ color: var(--bad); font-weight: 700; }}
    @media (max-width: 860px) {{ .split {{ grid-template-columns: 1fr; }} header {{ align-items: flex-start; flex-direction: column; }} }}
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
      <button id="plan">Plan First Checks</button>
      <button id="run-demo">Run Demo</button>
      <button id="replay" class="secondary">Replay Waveforms</button>
      <button id="run-regression" class="secondary">Run Regression</button>
    </div>
  </header>
  <main>
    <section>
      <h2>Status</h2>
      <div id="status" class="grid"></div>
    </section>
    <div class="split">
      <section>
        <h2>Initial Plan</h2>
        <div id="plan-table" class="table-wrap"></div>
      </section>
      <section>
        <h2>Symptom</h2>
        <textarea id="symptom">{escape(state.symptom)}</textarea>
      </section>
    </div>
    <section>
      <h2>Import Board</h2>
      <div class="form-grid">
        <label>Format
          <select id="import-format">
            <option value="csv">CSV</option>
            <option value="bom">BOM CSV/TSV</option>
            <option value="kicad">KiCad XML</option>
          </select>
        </label>
        <label>Board ID
          <input id="import-board-id" value="console_import">
        </label>
        <label>Name
          <input id="import-name" value="Console Import">
        </label>
      </div>
      <textarea id="import-content" spellcheck="false">net_name,test_point,domain,risk_level,expected_voltage_min,expected_voltage_max,allowed_measurements,component,pin,component_type
VIN,TP1,power,medium,4.75,5.25,"dc_voltage,waveform",J1,1,connector
GND,TP2,ground,low,,,dc_voltage,J1,2,connector</textarea>
      <div class="toolbar" style="margin-top: 10px;"><button id="import-board">Import Board</button></div>
    </section>
    <section>
      <h2>Board Topology</h2>
      <div id="topology" class="table-wrap"></div>
    </section>
    <section>
      <h2>Waveform Replay</h2>
      <div id="waveforms" class="grid"></div>
    </section>
    <section>
      <h2>Result</h2>
      <div id="links" class="toolbar"></div>
      <pre id="output">Ready.</pre>
    </section>
  </main>
  <script>
    const statusEl = document.getElementById('status');
    const topologyEl = document.getElementById('topology');
    const planTableEl = document.getElementById('plan-table');
    const waveformsEl = document.getElementById('waveforms');
    const outputEl = document.getElementById('output');
    const linksEl = document.getElementById('links');
    const symptomEl = document.getElementById('symptom');
    const importFormatEl = document.getElementById('import-format');
    const importBoardIdEl = document.getElementById('import-board-id');
    const importNameEl = document.getElementById('import-name');
    const importContentEl = document.getElementById('import-content');

    function show(data) {{
      outputEl.textContent = JSON.stringify(data, null, 2);
    }}

    function escapeHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, char => ({{
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }}[char]));
    }}

    function riskPill(risk) {{
      const cls = risk === 'high' ? 'bad' : risk === 'medium' ? 'warn' : '';
      return `<span class="pill ${{cls}}">${{escapeHtml(risk || 'low')}}</span>`;
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

    function renderPlan(actions) {{
      if (!actions || actions.length === 0) {{
        planTableEl.innerHTML = '<div class="metric"><span>No plan yet</span><strong>Run Plan First Checks</strong></div>';
        return;
      }}
      planTableEl.innerHTML = `<table>
        <thead><tr><th>Step</th><th>Action</th><th>Target</th><th>Instrument</th><th>Risk</th><th>Reason</th></tr></thead>
        <tbody>${{actions.map((action, index) => `
          <tr>
            <td>${{index + 1}}</td>
            <td>${{escapeHtml(action.measurement_kind || action.type)}}</td>
            <td><code>${{escapeHtml(action.net || action.rail || '')}}</code>${{action.test_point ? ` / <code>${{escapeHtml(action.test_point)}}</code>` : ''}}</td>
            <td>${{escapeHtml(action.instrument_kind || 'bench')}}</td>
            <td>${{riskPill(action.risk_level)}}</td>
            <td>${{escapeHtml(action.reason)}}</td>
          </tr>`).join('')}}</tbody>
      </table>`;
    }}

    function renderTopology(board) {{
      const pointByNet = {{}};
      for (const point of board.test_points || []) {{
        if (!pointByNet[point.net]) pointByNet[point.net] = [];
        pointByNet[point.net].push(point.id);
      }}
      topologyEl.innerHTML = `<table>
        <thead><tr><th>Net</th><th>Domain</th><th>Risk</th><th>Expected</th><th>Test Points</th></tr></thead>
        <tbody>${{(board.nets || []).map(net => {{
          const expected = net.expected_voltage ? `${{net.expected_voltage.min}}-${{net.expected_voltage.max}} ${{net.expected_voltage.unit || 'V'}}` :
            net.expected_frequency ? `${{net.expected_frequency.min}}-${{net.expected_frequency.max}} ${{net.expected_frequency.unit || 'Hz'}}` : '';
          return `<tr>
            <td><code>${{escapeHtml(net.name)}}</code></td>
            <td>${{escapeHtml(net.domain || 'unknown')}}</td>
            <td>${{riskPill(net.risk_level)}}</td>
            <td>${{escapeHtml(expected)}}</td>
            <td>${{escapeHtml((pointByNet[net.name] || []).join(', '))}}</td>
          </tr>`;
        }}).join('')}}</tbody>
      </table>`;
    }}

    function renderWaveforms(data) {{
      const waveforms = data.waveforms || [];
      if (!data.ok || waveforms.length === 0) {{
        waveformsEl.innerHTML = `<div class="metric"><span>Waveforms</span><strong>${{escapeHtml(data.reason || 'Run Demo first')}}</strong></div>`;
        return;
      }}
      waveformsEl.innerHTML = waveforms.map(item => {{
        if (!item.ok) {{
          return `<div class="metric"><span>${{escapeHtml(item.artifact?.id || 'artifact')}}</span><strong>${{escapeHtml(item.reason)}}</strong></div>`;
        }}
        const samples = item.samples || [];
        const values = samples.map(sample => sample.voltage_V);
        const times = samples.map(sample => sample.t_s);
        const minV = Math.min(...values);
        const maxV = Math.max(...values);
        const minT = Math.min(...times);
        const maxT = Math.max(...times);
        const width = 520;
        const height = 140;
        const pad = 12;
        const points = samples.map(sample => {{
          const x = pad + ((sample.t_s - minT) / Math.max(maxT - minT, 1e-12)) * (width - pad * 2);
          const y = height - pad - ((sample.voltage_V - minV) / Math.max(maxV - minV, 1e-12)) * (height - pad * 2);
          return `${{x.toFixed(2)}},${{y.toFixed(2)}}`;
        }}).join(' ');
        const measurement = item.measurement || {{}};
        const features = measurement.features || {{}};
        return `<div class="metric">
          <span><code>${{escapeHtml(measurement.target?.net || item.artifact?.id || 'waveform')}}</code></span>
          <svg viewBox="0 0 ${{width}} ${{height}}" width="100%" height="140" role="img" aria-label="Waveform preview">
            <rect x="0" y="0" width="${{width}}" height="${{height}}" fill="#fbfcfd" stroke="#d9dfe7"></rect>
            <polyline fill="none" stroke="#0f766e" stroke-width="2" points="${{points}}"></polyline>
          </svg>
          <div class="muted">${{item.sample_count}} samples, vpp=${{escapeHtml(features.v_pp_V ?? '')}} V, avg=${{escapeHtml(features.v_avg_V ?? '')}} V</div>
        </div>`;
      }}).join('');
    }}

    async function refresh() {{
      const [statusResponse, boardResponse] = await Promise.all([fetch('/api/status'), fetch('/api/board')]);
      const data = await statusResponse.json();
      const board = await boardResponse.json();
      statusEl.innerHTML = [
        ['Board', data.board.id],
        ['Nets', data.board.nets],
        ['Components', data.board.components],
        ['Test Points', data.board.test_points],
        ['Rails', data.board.rails],
        ['Model', data.model.backend]
      ].map(([label, value]) => `<div class="metric"><span>${{label}}</span><strong>${{value}}</strong></div>`).join('');
      renderTopology(board);
      show(data);
    }}

    async function runPlan() {{
      const response = await fetch('/api/plan', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ max_actions: 8, risk_ceiling: 'medium', include_power_off: true }})
      }});
      const data = await response.json();
      renderPlan(data.next_actions || []);
      setLinks({{}});
      show(data);
      await refresh();
    }}

    async function importBoard() {{
      const response = await fetch('/api/import-board', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          format: importFormatEl.value,
          board_id: importBoardIdEl.value,
          name: importNameEl.value,
          content: importContentEl.value
        }})
      }});
      const data = await response.json();
      setLinks({{}});
      show(data);
      if (data.ok) {{
        await refresh();
      }}
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
      await replayWaveforms();
      await refresh();
    }}

    async function replayWaveforms() {{
      const response = await fetch('/api/replay?max_points=180');
      const data = await response.json();
      renderWaveforms(data);
      show(data);
    }}

    async function runRegression() {{
      const response = await fetch('/api/regression', {{ method: 'POST' }});
      const data = await response.json();
      setLinks({{}});
      show(data);
      await refresh();
    }}

    document.getElementById('refresh').addEventListener('click', refresh);
    document.getElementById('import-board').addEventListener('click', importBoard);
    document.getElementById('plan').addEventListener('click', runPlan);
    document.getElementById('run-demo').addEventListener('click', runDemo);
    document.getElementById('replay').addEventListener('click', replayWaveforms);
    document.getElementById('run-regression').addEventListener('click', runRegression);
    refresh();
  </script>
</body>
</html>
"""


def _compact_last(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    if "import" in payload:
        imported = payload.get("import") or {}
        return {
            "ok": payload.get("ok"),
            "board_path": payload.get("board_path"),
            "board_id": imported.get("board_id"),
            "counts": imported.get("counts"),
        }
    if "report_url" in payload:
        return {"ok": payload.get("ok"), "report_url": payload.get("report_url")}
    return {
        "ok": payload.get("ok"),
        "passed": payload.get("passed"),
        "failed": payload.get("failed"),
        "count": payload.get("count"),
    }


def _safe_identifier(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value.strip())
    cleaned = cleaned.strip("_-")
    return cleaned or "imported_board"


def _resolve_console_artifact_path(uri: Any, artifact_dir: Path, session_dir: Path) -> Path | None:
    if not isinstance(uri, str) or not uri:
        return None
    path = Path(uri)
    candidates = [path]
    if not path.is_absolute():
        candidates.extend([session_dir / path, artifact_dir / path])
    root = artifact_dir.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.exists() or not resolved.is_file():
            continue
        try:
            resolved.relative_to(root)
        except ValueError:
            if path.is_absolute():
                continue
        return resolved
    return None


def _read_waveform_csv(path: Path) -> list[dict[str, float]]:
    samples: list[dict[str, float]] = []
    with path.open("r", encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
        if header[:2] != ["t_s", "voltage_V"]:
            raise ValueError("Unsupported waveform CSV header")
        for line in handle:
            if not line.strip():
                continue
            t_s, voltage_v = line.strip().split(",", 1)
            samples.append({"t_s": float(t_s), "voltage_V": float(voltage_v)})
    return samples


def _decimate_samples(samples: list[dict[str, float]], max_points: int) -> list[dict[str, float]]:
    if len(samples) <= max_points:
        return samples
    step = (len(samples) - 1) / max(max_points - 1, 1)
    result = []
    used_indexes: set[int] = set()
    for index in range(max_points):
        source_index = round(index * step)
        if source_index in used_indexes:
            continue
        used_indexes.add(source_index)
        result.append(samples[source_index])
    return result


def _measurement_for_artifact(session: dict[str, Any], artifact_id: str) -> dict[str, Any] | None:
    for measurement in session.get("measurements", []) or []:
        if artifact_id in (measurement.get("artifact_ids") or []):
            return measurement
    return None

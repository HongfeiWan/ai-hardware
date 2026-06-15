"""Dependency-free HTML report generation for diagnostic sessions."""

from __future__ import annotations

import base64
from html import escape
import json
from pathlib import Path
from typing import Any

from .data import load_document


def generate_session_report(
    session_path: str | Path,
    output: str | Path,
    audit_path: str | Path | None = None,
) -> dict[str, Any]:
    session_file = Path(session_path)
    session = load_document(session_file)
    audit_events = _load_audit(audit_path)
    html = _render_report(session, session_file, audit_events)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return {
        "ok": True,
        "path": str(target),
        "session_id": session.get("session_id"),
        "finding_count": len(session.get("findings", []) or []),
        "measurement_count": len(session.get("measurements", []) or []),
        "audit_count": len(audit_events),
    }


def _render_report(session: dict[str, Any], session_path: Path, audit_events: list[dict[str, Any]]) -> str:
    title = f"AI Hardware Report - {session.get('session_id', 'session')}"
    findings = session.get("findings", []) or []
    measurements = session.get("measurements", []) or []
    actions = session.get("next_actions", []) or []
    artifacts = session.get("artifacts", []) or []
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #5d6875;
      --line: #d7dde5;
      --accent: #0f766e;
      --warn: #b45309;
      --fault: #b91c1c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
    }}
    header {{
      padding: 24px 28px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{ margin: 0 0 6px; font-size: 24px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 12px; font-size: 17px; letter-spacing: 0; }}
    main {{ padding: 20px 28px 36px; display: grid; gap: 18px; }}
    section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }}
    .meta {{ color: var(--muted); display: flex; flex-wrap: wrap; gap: 10px 18px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
    .metric {{ border: 1px solid var(--line); border-radius: 6px; padding: 12px; }}
    .metric strong {{ display: block; font-size: 20px; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    .severity-warning {{ color: var(--warn); font-weight: 700; }}
    .severity-fault, .severity-critical {{ color: var(--fault); font-weight: 700; }}
    .severity-info {{ color: var(--accent); font-weight: 700; }}
    .pre {{ white-space: pre-wrap; word-break: break-word; }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(str(session.get("board_id", "Unknown board")))}</h1>
    <div class="meta">
      <span>Session <code>{escape(str(session.get("session_id", "")))}</code></span>
      <span>Started {escape(str(session.get("started_at", "")))}</span>
      <span>Source <code>{escape(str(session_path))}</code></span>
    </div>
  </header>
  <main>
    <section>
      <h2>Summary</h2>
      <div class="grid">
        <div class="metric">Measurements<strong>{len(measurements)}</strong></div>
        <div class="metric">Findings<strong>{len(findings)}</strong></div>
        <div class="metric">Next Actions<strong>{len(actions)}</strong></div>
        <div class="metric">Artifacts<strong>{len(artifacts)}</strong></div>
      </div>
      <p>{escape(str(session.get("observed_symptom", "")))}</p>
    </section>
    <section>
      <h2>Findings</h2>
      {_findings_table(findings)}
    </section>
    <section>
      <h2>Measurements</h2>
      {_measurements_table(measurements)}
    </section>
    <section>
      <h2>Next Actions</h2>
      {_actions_table(actions)}
    </section>
    <section>
      <h2>Artifacts</h2>
      {_artifacts_table(artifacts, session_path.parent)}
    </section>
    <section>
      <h2>Audit</h2>
      {_audit_table(audit_events)}
    </section>
  </main>
</body>
</html>
"""


def _findings_table(findings: list[dict[str, Any]]) -> str:
    rows = []
    for finding in findings:
        severity = escape(str(finding.get("severity", "info")))
        rows.append(
            "<tr>"
            f"<td><code>{escape(str(finding.get('id', '')))}</code></td>"
            f"<td class=\"severity-{severity}\">{severity}</td>"
            f"<td>{escape(str(finding.get('confidence', '')))}</td>"
            f"<td>{escape(str(finding.get('summary', '')))}</td>"
            f"<td>{escape('; '.join(finding.get('evidence', []) or []))}</td>"
            "</tr>"
        )
    return _table(["ID", "Severity", "Confidence", "Summary", "Evidence"], rows)


def _measurements_table(measurements: list[dict[str, Any]]) -> str:
    rows = []
    for measurement in measurements:
        rows.append(
            "<tr>"
            f"<td><code>{escape(str(measurement.get('id', '')))}</code></td>"
            f"<td>{escape(str(measurement.get('kind', '')))}</td>"
            f"<td>{escape(str((measurement.get('target') or {}).get('net', '')))}</td>"
            f"<td><code>{escape(str(measurement.get('instrument_id', '')))}</code></td>"
            f"<td class=\"pre\">{escape(json.dumps(measurement.get('features', {}), ensure_ascii=False))}</td>"
            "</tr>"
        )
    return _table(["ID", "Kind", "Net", "Instrument", "Features"], rows)


def _actions_table(actions: list[dict[str, Any]]) -> str:
    rows = []
    for action in actions:
        risk = escape(str(action.get("risk_level", "")))
        rows.append(
            "<tr>"
            f"<td>{escape(str(action.get('type', '')))}</td>"
            f"<td>{escape(str(action.get('net', '')))}</td>"
            f"<td>{risk}</td>"
            f"<td>{escape(str(action.get('requires_confirmation', False)))}</td>"
            f"<td>{escape(str(action.get('reason', '')))}</td>"
            "</tr>"
        )
    return _table(["Type", "Net", "Risk", "Confirm", "Reason"], rows)


def _artifacts_table(artifacts: list[dict[str, Any]], base_dir: Path | None = None) -> str:
    rows = []
    for artifact in artifacts:
        preview = _artifact_preview(artifact, base_dir)
        rows.append(
            "<tr>"
            f"<td><code>{escape(str(artifact.get('id', '')))}</code></td>"
            f"<td>{escape(str(artifact.get('kind', '')))}</td>"
            f"<td><code>{escape(str(artifact.get('uri', '')))}</code></td>"
            f"<td><code>{escape(str(artifact.get('sha256', '')))}</code></td>"
            f"<td>{preview}</td>"
            "</tr>"
        )
    return _table(["ID", "Kind", "URI", "SHA-256", "Preview"], rows)


def _audit_table(events: list[dict[str, Any]]) -> str:
    rows = []
    for event in events[-100:]:
        safety = event.get("safety") or {}
        rows.append(
            "<tr>"
            f"<td>{escape(str(event.get('timestamp', '')))}</td>"
            f"<td><code>{escape(str(event.get('tool', '')))}</code></td>"
            f"<td>{escape(str(event.get('outcome', '')))}</td>"
            f"<td>{escape(str(safety.get('risk_level', '')))}</td>"
            f"<td class=\"pre\">{escape('; '.join(safety.get('reasons', []) or []))}</td>"
            "</tr>"
        )
    return _table(["Time", "Tool", "Outcome", "Risk", "Reasons"], rows)


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    body = "".join(rows) if rows else f"<tr><td colspan=\"{len(headers)}\">No data</td></tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _load_audit(path: str | Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    source = Path(path)
    if not source.exists():
        return []
    return [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]


def _artifact_preview(artifact: dict[str, Any], base_dir: Path | None) -> str:
    path = _resolve_artifact_path(str(artifact.get("uri", "")), base_dir)
    if path is None:
        return "<span class=\"muted\">missing</span>"
    if artifact.get("kind") == "waveform_csv":
        try:
            samples = _read_waveform_csv(path)
        except Exception:
            return "<span class=\"muted\">unreadable</span>"
        if not samples:
            return "<span class=\"muted\">empty</span>"
        return _waveform_svg(samples)
    if artifact.get("kind") == "logic_csv":
        try:
            samples = _read_logic_csv(path)
        except Exception:
            return "<span class=\"muted\">unreadable</span>"
        if not samples:
            return "<span class=\"muted\">empty</span>"
        return _logic_svg(samples)
    if artifact.get("kind") == "scope_screenshot":
        return _image_preview(path, str(artifact.get("mime_type", "application/octet-stream")))
    return ""


def _resolve_artifact_path(uri: str, base_dir: Path | None) -> Path | None:
    if not uri:
        return None
    path = Path(uri)
    candidates = [path]
    if not path.is_absolute() and base_dir is not None:
        candidates.append(base_dir / path)
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _read_waveform_csv(path: Path) -> list[tuple[float, float]]:
    samples: list[tuple[float, float]] = []
    with path.open("r", encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
        if header[:2] != ["t_s", "voltage_V"]:
            return []
        for line in handle:
            if not line.strip():
                continue
            t_s, voltage_v = line.strip().split(",", 1)
            samples.append((float(t_s), float(voltage_v)))
    return samples


def _read_logic_csv(path: Path) -> list[tuple[float, int]]:
    samples: list[tuple[float, int]] = []
    with path.open("r", encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
        if header[:2] != ["t_s", "level"]:
            return []
        for line in handle:
            if not line.strip():
                continue
            t_s, level = line.strip().split(",", 1)
            samples.append((float(t_s), int(level)))
    return samples


def _waveform_svg(samples: list[tuple[float, float]]) -> str:
    width = 220
    height = 64
    padding = 6
    stride = max(1, len(samples) // 160)
    reduced = samples[::stride]
    t_values = [point[0] for point in reduced]
    v_values = [point[1] for point in reduced]
    t_min, t_max = min(t_values), max(t_values)
    v_min, v_max = min(v_values), max(v_values)
    if t_max == t_min:
        t_max = t_min + 1.0
    if v_max == v_min:
        v_max = v_min + 1.0
    points = []
    for t_s, voltage_v in reduced:
        x = padding + (t_s - t_min) / (t_max - t_min) * (width - padding * 2)
        y = height - padding - (voltage_v - v_min) / (v_max - v_min) * (height - padding * 2)
        points.append(f"{x:.1f},{y:.1f}")
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" width=\"{width}\" height=\"{height}\" "
        "role=\"img\" aria-label=\"Waveform preview\">"
        f"<rect x=\"0\" y=\"0\" width=\"{width}\" height=\"{height}\" rx=\"6\" fill=\"#f8fafc\" stroke=\"#d7dde5\"/>"
        f"<polyline points=\"{' '.join(points)}\" fill=\"none\" stroke=\"#0f766e\" stroke-width=\"2\"/>"
        f"<text x=\"6\" y=\"58\" font-size=\"10\" fill=\"#5d6875\">{v_min:.3g}..{v_max:.3g} V</text>"
        "</svg>"
    )


def _logic_svg(samples: list[tuple[float, int]]) -> str:
    width = 220
    height = 64
    padding = 6
    stride = max(1, len(samples) // 160)
    reduced = samples[::stride]
    t_values = [point[0] for point in reduced]
    t_min, t_max = min(t_values), max(t_values)
    if t_max == t_min:
        t_max = t_min + 1.0
    points = []
    for t_s, level in reduced:
        x = padding + (t_s - t_min) / (t_max - t_min) * (width - padding * 2)
        y = 18 if level else 46
        points.append(f"{x:.1f},{y:.1f}")
    return (
        f"<svg viewBox=\"0 0 {width} {height}\" width=\"{width}\" height=\"{height}\" "
        "role=\"img\" aria-label=\"Logic preview\">"
        f"<rect x=\"0\" y=\"0\" width=\"{width}\" height=\"{height}\" rx=\"6\" fill=\"#f8fafc\" stroke=\"#d7dde5\"/>"
        f"<polyline points=\"{' '.join(points)}\" fill=\"none\" stroke=\"#2563eb\" stroke-width=\"2\"/>"
        "<text x=\"6\" y=\"58\" font-size=\"10\" fill=\"#5d6875\">logic level</text>"
        "</svg>"
    )


def _image_preview(path: Path, mime_type: str) -> str:
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception:
        return "<span class=\"muted\">unreadable</span>"
    safe_mime = escape(mime_type)
    return (
        f"<img src=\"data:{safe_mime};base64,{encoded}\" alt=\"Scope screenshot\" "
        "style=\"max-width:240px;max-height:160px;border:1px solid #d7dde5;border-radius:6px;background:#0b1118\"/>"
    )

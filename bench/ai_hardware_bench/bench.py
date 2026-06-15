"""Bench-side tool implementation for hardware diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .data import BoardContext, DiagnosticSession, load_board_context, utc_now
from .instruments import (
    MockFixture,
    build_dmm_driver,
    build_logic_analyzer_driver,
    build_psu_driver,
    build_scope_driver,
    extract_waveform_features,
)
from .model import build_model_adapter
from .safety import AuditLogger, SafetyPolicy
from .session import validate_session, validate_session_file
from .topology import Topology


ToolFunc = Callable[..., dict[str, Any]]


class BenchApp:
    """A dependency-free bench prototype with MCP-shaped tools."""

    def __init__(
        self,
        artifact_dir: str | Path = "artifacts/mock-bench",
        instrument_config: dict[str, Any] | str | Path | None = None,
        model_config: dict[str, Any] | str | Path | None = None,
        audit_path: str | Path | None = None,
    ) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.audit = AuditLogger(audit_path or self.artifact_dir / "audit.jsonl")
        self.safety = SafetyPolicy()
        self.board: BoardContext | None = None
        self.topology: Topology | None = None
        self.session: DiagnosticSession | None = None
        config = _load_instrument_config(instrument_config)
        model = _load_json_config(model_config, "model config")
        self.psu = build_psu_driver(config.get("psu"))
        self.scope = build_scope_driver(config.get("scope"))
        self.dmm = build_dmm_driver(config.get("dmm"))
        self.logic = build_logic_analyzer_driver(config.get("logic_analyzer"))
        self.fixture = MockFixture()
        self.model = build_model_adapter(model)
        self.tools: dict[str, ToolFunc] = {
            "load_board_context": self.load_board_context_tool,
            "instrument_status": self.instrument_status,
            "model_status": self.model_status,
            "safety_status": self.safety_status,
            "validate_session": self.validate_session_tool,
            "read_audit_log": self.read_audit_log,
            "plan_initial_measurements": self.plan_initial_measurements,
            "list_nets": self.list_nets,
            "trace_net_neighbors": self.trace_net_neighbors,
            "find_test_points": self.find_test_points,
            "trace_power_path": self.trace_power_path,
            "list_downstream_loads": self.list_downstream_loads,
            "set_power_rail": self.set_power_rail,
            "measure_dc_voltage": self.measure_dc_voltage,
            "measure_impedance": self.measure_impedance,
            "capture_waveform": self.capture_waveform,
            "capture_logic": self.capture_logic,
            "capture_scope_screenshot": self.capture_scope_screenshot,
            "extract_signal_features": self.extract_signal_features,
            "diagnose_hardware": self.diagnose_hardware,
            "suggest_next_probe": self.suggest_next_probe,
            "esp32_set_mux": self.esp32_set_mux,
            "esp32_reset_dut": self.esp32_reset_dut,
        }

    def require_board(self) -> BoardContext:
        if self.board is None:
            raise RuntimeError("No board context loaded. Call load_board_context first.")
        return self.board

    def require_topology(self) -> Topology:
        if self.topology is None:
            raise RuntimeError("No board context loaded. Call load_board_context first.")
        return self.topology

    def require_session(self) -> DiagnosticSession:
        if self.session is None:
            board = self.require_board()
            self.session = DiagnosticSession(board.board_id, "unspecified symptom")
            self._sync_session_instruments()
        return self.session

    def load_board_context_tool(
        self,
        path: str,
        observed_symptom: str = "unspecified symptom",
        session_id: str | None = None,
        operator: str = "bench",
    ) -> dict[str, Any]:
        board = load_board_context(path)
        self.board = board
        self.topology = Topology(board)
        generated_session_id = session_id or f"session_{board.board_id}_{_timestamp_id()}"
        self.session = DiagnosticSession(board.board_id, observed_symptom, generated_session_id, operator)
        self._sync_session_instruments()
        return {
            "ok": True,
            "board_id": board.board_id,
            "board_name": board.data["board"]["name"],
            "source_path": str(board.source_path) if board.source_path else None,
            "counts": {
                "nets": len(board.nets),
                "components": len(board.components),
                "test_points": len(board.test_points),
                "rails": len(board.rails),
            },
            "session_id": self.session.session_id,
        }

    def instrument_status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "instruments": [
                self.psu.status(),
                self.scope.status(),
                self.dmm.status(),
                self.logic.status(),
                {"id": "mock_fixture", "kind": "esp32_fixture", "backend": "mock"},
            ],
        }

    def model_status(self) -> dict[str, Any]:
        return {"ok": True, "model": self.model.status()}

    def safety_status(self) -> dict[str, Any]:
        return {"ok": True, "policy": self.safety.status(), "audit_path": str(self.audit.path)}

    def validate_session_tool(self, path: str | None = None, check_artifacts: bool = True) -> dict[str, Any]:
        if path:
            return validate_session_file(path, check_artifacts=check_artifacts)
        session = self.require_session()
        errors = validate_session(session.data, check_artifacts=check_artifacts)
        return {"ok": not errors, "session_id": session.session_id, "errors": errors}

    def read_audit_log(self, limit: int | None = 50) -> dict[str, Any]:
        events = self.audit.read(limit=limit)
        return {"ok": True, "events": events, "count": len(events)}

    def plan_initial_measurements(
        self,
        max_actions: int = 8,
        risk_ceiling: str = "medium",
        include_power_off: bool = True,
    ) -> dict[str, Any]:
        board = self.require_board()
        session = self.require_session()
        risk_order = {"low": 0, "medium": 1, "high": 2}
        if risk_ceiling not in risk_order:
            raise ValueError("risk_ceiling must be low, medium or high")
        limit = max(1, min(int(max_actions), 20))
        actions: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None, str | None]] = set()

        def within_risk(net: str) -> bool:
            risk = board.nets[net].get("risk_level", "low")
            return risk_order.get(risk, 0) <= risk_order[risk_ceiling]

        def add(action: dict[str, Any]) -> None:
            if len(actions) >= limit:
                return
            key = (str(action.get("type")), action.get("net"), action.get("measurement_kind"))
            if key in seen:
                return
            seen.add(key)
            actions.append(action)

        rails = sorted(board.rails.values(), key=lambda rail: (rail.get("startup_order", 999), rail["name"]))
        if include_power_off:
            for rail in rails:
                output_net = rail["output_net"]
                if output_net in board.nets and within_risk(output_net):
                    point = self._resolve_test_point(output_net, None, "impedance", required=False)
                    if point:
                        add(
                            {
                                "type": "measure_net",
                                "net": output_net,
                                "test_point": point["id"],
                                "instrument_kind": "dmm",
                                "measurement_kind": "impedance",
                                "power_state": "off",
                                "reason": "Check rail-to-ground impedance before applying power.",
                                "risk_level": board.nets[output_net].get("risk_level", "low"),
                                "requires_confirmation": False,
                            }
                        )

        for rail in rails:
            output_net = rail["output_net"]
            if output_net in board.nets and rail.get("source_net") not in board.nets and within_risk(output_net):
                current_limit = _conservative_current_limit(rail)
                add(
                    {
                        "type": "change_power_state",
                        "rail": rail["name"],
                        "net": output_net,
                        "voltage_V": float(rail.get("nominal_voltage", 0.0)),
                        "current_limit_A": current_limit,
                        "output": True,
                        "reason": "Apply the primary input rail with a conservative current limit.",
                        "risk_level": board.nets[output_net].get("risk_level", "low"),
                        "requires_confirmation": False,
                    }
                )

        for rail in rails:
            for net_key, reason in (
                ("output_net", "Measure rail DC voltage after conservative power-up."),
                ("enable_net", "Confirm the rail enable signal is in the expected state."),
                ("power_good_net", "Check the rail power-good status before deeper probing."),
            ):
                net = rail.get(net_key)
                if not net or net not in board.nets or not within_risk(net):
                    continue
                point = self._resolve_test_point(net, None, "dc_voltage", required=False)
                add(
                    {
                        "type": "measure_net",
                        "net": net,
                        "test_point": point.get("id") if point else None,
                        "instrument_kind": "dmm",
                        "measurement_kind": "dc_voltage",
                        "reason": reason,
                        "risk_level": board.nets[net].get("risk_level", "low"),
                        "requires_confirmation": False,
                    }
                )

        if not actions:
            for point in sorted(board.test_points.values(), key=lambda item: item["id"]):
                net = point["net"]
                if within_risk(net):
                    add(
                        {
                            "type": "measure_net",
                            "net": net,
                            "test_point": point["id"],
                            "instrument_kind": "dmm",
                            "measurement_kind": "dc_voltage",
                            "reason": "Collect a low-risk voltage check before deeper probing.",
                            "risk_level": board.nets[net].get("risk_level", "low"),
                            "requires_confirmation": False,
                        }
                    )

        session.set_next_actions(actions)
        return {"ok": True, "next_actions": actions, "count": len(actions), "risk_ceiling": risk_ceiling}

    def list_nets(self, domain: str | None = None, risk_level: str | None = None) -> dict[str, Any]:
        nets = self.require_topology().list_nets(domain=domain, risk_level=risk_level)
        return {"ok": True, "nets": nets, "count": len(nets)}

    def trace_net_neighbors(self, net: str, depth: int = 1) -> dict[str, Any]:
        trace = self.require_topology().trace_net_neighbors(net, depth=depth)
        return {"ok": True, **trace}

    def find_test_points(
        self,
        net: str | None = None,
        measurement: str | None = None,
        risk_level: str | None = None,
    ) -> dict[str, Any]:
        points = self.require_topology().find_test_points(net=net, measurement=measurement, risk_level=risk_level)
        return {"ok": True, "test_points": points, "count": len(points)}

    def trace_power_path(self, net: str) -> dict[str, Any]:
        return {"ok": True, **self.require_topology().trace_power_path(net)}

    def list_downstream_loads(
        self,
        rail: str | None = None,
        net: str | None = None,
        depth: int = 2,
    ) -> dict[str, Any]:
        return {"ok": True, **self.require_topology().list_downstream_loads(rail=rail, net=net, depth=depth)}

    def set_power_rail(
        self,
        rail: str,
        voltage_V: float,
        current_limit_A: float,
        output: bool = True,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        board = self.require_board()
        session = self.require_session()
        rail_info = board.rails.get(rail)
        if rail_info is None:
            raise ValueError(f"Unknown rail: {rail}")
        self._validate_power_request(rail_info, voltage_V, current_limit_A)
        result = {
            "dry_run": dry_run,
            "rail": rail,
            "target_net": rail_info["output_net"],
            "safety_checked": True,
        }
        if dry_run:
            result.update(
                {
                    "voltage_V": round(voltage_V if output else 0.0, 6),
                    "current_limit_A": round(current_limit_A, 6),
                    "output": output,
                    "mock": True,
                }
            )
        else:
            result.update(self.psu.set_power(voltage_V, current_limit_A, output))
        measurement = {
            "id": self._next_measurement_id(),
            "timestamp": utc_now(),
            "kind": "current",
            "target": {"net": rail_info["output_net"]},
            "instrument_id": self.psu.id,
            "settings": {
                "rail": rail,
                "voltage_V": voltage_V,
                "current_limit_A": current_limit_A,
                "output": output,
                "dry_run": dry_run,
            },
            "result": result,
            "features": {
                "current_limited": bool(result.get("current_limited", False)),
                "within_configured_limits": True,
            },
        }
        session.add_measurement(measurement)
        return {"ok": True, "measurement": measurement}

    def measure_dc_voltage(self, net: str, test_point: str | None = None) -> dict[str, Any]:
        board = self.require_board()
        session = self.require_session()
        net_name = board.canonical_net(net)
        point = self._resolve_test_point(net_name, test_point, "dc_voltage", required=False)
        captured = self.dmm.measure_dc_voltage(
            net_name,
            board.nets[net_name].get("expected_voltage"),
            session.data.get("observed_symptom", ""),
        )
        measurement = {
            "id": self._next_measurement_id(),
            "timestamp": utc_now(),
            "kind": "dc_voltage",
            "target": _measurement_target(net_name, point),
            "instrument_id": self.dmm.id,
            "settings": {"mode": "dc_voltage"},
            "result": captured["result"],
            "features": captured["features"],
        }
        session.add_measurement(measurement)
        return {"ok": True, "measurement": measurement}

    def measure_impedance(
        self,
        net: str,
        test_point: str | None = None,
        power_state: str = "off",
    ) -> dict[str, Any]:
        if power_state != "off":
            raise ValueError("Impedance measurements require power_state='off'")
        board = self.require_board()
        session = self.require_session()
        net_name = board.canonical_net(net)
        point = self._resolve_test_point(net_name, test_point, "impedance")
        captured = self.dmm.measure_impedance(
            net_name,
            board.nets[net_name],
            session.data.get("observed_symptom", ""),
        )
        measurement = {
            "id": self._next_measurement_id(),
            "timestamp": utc_now(),
            "kind": "impedance",
            "target": _measurement_target(net_name, point),
            "instrument_id": self.dmm.id,
            "settings": {"mode": "resistance_2w", "power_state": power_state},
            "result": captured["result"],
            "features": captured["features"],
        }
        session.add_measurement(measurement)
        return {"ok": True, "measurement": measurement}

    def capture_waveform(
        self,
        net: str,
        test_point: str | None = None,
        sample_count: int = 1000,
        duration_s: float = 0.01,
    ) -> dict[str, Any]:
        board = self.require_board()
        session = self.require_session()
        net_name = board.canonical_net(net)
        point = self._resolve_test_point(net_name, test_point, "waveform")
        artifact_id = f"artifact_{self._next_measurement_id()}_{net_name.lower()}"
        artifact_path = self.artifact_dir / session.session_id / f"{artifact_id}.csv"
        captured = self.scope.capture_waveform(
            net_name,
            board.nets[net_name].get("expected_voltage"),
            session.data.get("observed_symptom", ""),
            sample_count,
            duration_s,
            artifact_path,
        )
        measurement_id = self._next_measurement_id()
        artifact = {
            "id": artifact_id,
            "kind": "waveform_csv",
            "uri": str(artifact_path),
            "mime_type": "text/csv",
            "sha256": _sha256_file(artifact_path),
        }
        session.add_artifact(artifact)
        measurement = {
            "id": measurement_id,
            "timestamp": utc_now(),
            "kind": "waveform",
            "target": {"net": net_name, "test_point": point.get("id")},
            "instrument_id": self.scope.id,
            "settings": {
                "sample_count": captured["sample_count"],
                "duration_s": captured["duration_s"],
            },
            "result": {"artifact_id": artifact_id},
            "features": captured["features"],
            "artifact_ids": [artifact_id],
        }
        session.add_measurement(measurement)
        return {"ok": True, "measurement": measurement, "artifact": artifact}

    def capture_logic(
        self,
        net: str,
        test_point: str | None = None,
        sample_count: int = 256,
        duration_s: float = 0.05,
    ) -> dict[str, Any]:
        board = self.require_board()
        session = self.require_session()
        net_name = board.canonical_net(net)
        point = self._resolve_test_point(net_name, test_point, "logic", required=False)
        artifact_id = f"artifact_{self._next_measurement_id()}_{net_name.lower()}_logic"
        artifact_path = self.artifact_dir / session.session_id / f"{artifact_id}.csv"
        captured = self.logic.capture_logic(
            net_name,
            session.data.get("observed_symptom", ""),
            sample_count,
            duration_s,
            artifact_path,
        )
        measurement_id = self._next_measurement_id()
        artifact = {
            "id": artifact_id,
            "kind": "logic_csv",
            "uri": str(artifact_path),
            "mime_type": "text/csv",
            "sha256": _sha256_file(artifact_path),
        }
        session.add_artifact(artifact)
        measurement = {
            "id": measurement_id,
            "timestamp": utc_now(),
            "kind": "logic",
            "target": _measurement_target(net_name, point),
            "instrument_id": self.logic.id,
            "settings": {
                "sample_count": captured["sample_count"],
                "duration_s": captured["duration_s"],
            },
            "result": {"artifact_id": artifact_id},
            "features": captured["features"],
            "artifact_ids": [artifact_id],
        }
        session.add_measurement(measurement)
        return {"ok": True, "measurement": measurement, "artifact": artifact}

    def extract_signal_features(self, artifact_id: str | None = None, uri: str | None = None) -> dict[str, Any]:
        session = self.require_session()
        artifact: dict[str, Any] | None = None
        if artifact_id:
            artifact = next((item for item in session.data["artifacts"] if item.get("id") == artifact_id), None)
            if artifact is None:
                raise ValueError(f"Unknown artifact_id: {artifact_id}")
            uri = artifact["uri"]
        if uri is None:
            raise ValueError("Provide artifact_id or uri")
        samples: list[tuple[float, float]] = []
        with Path(uri).open("r", encoding="utf-8") as handle:
            header = handle.readline().strip().split(",")
            if header[:2] != ["t_s", "voltage_V"]:
                raise ValueError(f"Unsupported waveform CSV header in {uri}")
            for line in handle:
                if not line.strip():
                    continue
                t_s, voltage_v = line.strip().split(",", 1)
                samples.append((float(t_s), float(voltage_v)))
        features = extract_waveform_features(samples)
        return {"ok": True, "features": features, "sample_count": len(samples), "artifact_id": artifact_id}

    def capture_scope_screenshot(
        self,
        net: str,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        board = self.require_board()
        session = self.require_session()
        net_name = board.canonical_net(net)
        measurement = None
        features = None
        if artifact_id:
            measurement = next(
                (
                    item
                    for item in session.data["measurements"]
                    if artifact_id in (item.get("artifact_ids") or [])
                ),
                None,
            )
            if measurement is None:
                raise ValueError(f"Unknown waveform artifact_id: {artifact_id}")
            features = measurement.get("features")
        else:
            measurement = next(
                (
                    item
                    for item in reversed(session.data["measurements"])
                    if item.get("kind") == "waveform" and item.get("target", {}).get("net") == net_name
                ),
                None,
            )
            features = measurement.get("features") if measurement else None
        screenshot_artifact_id = f"artifact_{self._next_measurement_id()}_{net_name.lower()}_scope"
        screenshot_suffix = ".svg" if self.scope.status().get("backend") == "mock" else ".png"
        screenshot_path = self.artifact_dir / session.session_id / f"{screenshot_artifact_id}{screenshot_suffix}"
        captured = self.scope.capture_screenshot(net_name, features, screenshot_path)
        artifact = {
            "id": screenshot_artifact_id,
            "kind": "scope_screenshot",
            "uri": str(captured["artifact_path"]),
            "mime_type": captured.get("mime_type", "image/svg+xml"),
            "sha256": _sha256_file(Path(captured["artifact_path"])),
        }
        session.add_artifact(artifact)
        shot_measurement = {
            "id": self._next_measurement_id(),
            "timestamp": utc_now(),
            "kind": "waveform",
            "target": {"net": net_name, "test_point": (measurement or {}).get("target", {}).get("test_point")},
            "instrument_id": self.scope.id,
            "settings": {"capture_type": "scope_screenshot", "source_artifact_id": artifact_id},
            "result": {
                "artifact_id": screenshot_artifact_id,
                "width_px": captured.get("width_px"),
                "height_px": captured.get("height_px"),
            },
            "features": features or {},
            "artifact_ids": [screenshot_artifact_id],
        }
        session.add_measurement(shot_measurement)
        return {"ok": True, "measurement": shot_measurement, "artifact": artifact}

    def diagnose_hardware(self) -> dict[str, Any]:
        board = self.require_board()
        session = self.require_session()
        result = self.model.analyze(board, session, self.require_topology())
        finding = result["finding"]
        next_actions = result["next_actions"]
        session.add_finding(finding)
        session.set_next_actions(next_actions)
        return {"ok": True, "finding": finding, "next_actions": next_actions}

    def suggest_next_probe(self, net: str | None = None) -> dict[str, Any]:
        board = self.require_board()
        session = self.require_session()
        if session.data["next_actions"]:
            return {"ok": True, "next_actions": session.data["next_actions"]}
        if net:
            net_name = board.canonical_net(net)
            trace = self.require_topology().trace_net_neighbors(net_name, depth=1)
            candidates = [item["name"] for item in trace["neighbor_nets"]]
        else:
            candidates = [name for name, item in board.nets.items() if item.get("risk_level") != "high"]
        actions = []
        for candidate in candidates[:3]:
            point = self._resolve_test_point(candidate, None, "dc_voltage", required=False)
            actions.append(
                {
                    "type": "measure_net",
                    "net": candidate,
                    "test_point": point.get("id") if point else None,
                    "instrument_kind": "dmm",
                    "reason": "Collect a low-risk voltage check before deeper probing.",
                    "risk_level": board.nets[candidate].get("risk_level", "low"),
                    "requires_confirmation": board.nets[candidate].get("risk_level") == "high",
                }
            )
        session.set_next_actions(actions)
        return {"ok": True, "next_actions": actions}

    def esp32_set_mux(self, channel: int, dry_run: bool = True) -> dict[str, Any]:
        if channel < 0 or channel > 31:
            raise ValueError("MUX channel must be between 0 and 31")
        result = {"ok": True, "mux_channel": channel, "dry_run": dry_run, "mock": True}
        if not dry_run:
            result = self.fixture.set_mux(channel)
        self.require_session().add_measurement(
            {
                "id": self._next_measurement_id(),
                "timestamp": utc_now(),
                "kind": "fixture_state",
                "target": {"net": "fixture"},
                "instrument_id": "mock_fixture",
                "settings": {"channel": channel, "dry_run": dry_run},
                "result": result,
            }
        )
        return result

    def esp32_reset_dut(self, pulse_ms: int = 100, dry_run: bool = True) -> dict[str, Any]:
        if pulse_ms < 10 or pulse_ms > 5000:
            raise ValueError("pulse_ms must be between 10 and 5000")
        result = {"ok": True, "pulse_ms": pulse_ms, "dry_run": dry_run, "mock": True}
        if not dry_run:
            result = self.fixture.reset_dut(pulse_ms)
        self.require_session().add_measurement(
            {
                "id": self._next_measurement_id(),
                "timestamp": utc_now(),
                "kind": "fixture_state",
                "target": {"net": "fixture"},
                "instrument_id": "mock_fixture",
                "settings": {"pulse_ms": pulse_ms, "dry_run": dry_run},
                "result": result,
            }
        )
        return result

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")
        tool_arguments = dict(arguments or {})
        decision = self.safety.evaluate(name, tool_arguments, self.board)
        try:
            decision.require_allowed()
            call_arguments = dict(tool_arguments)
            call_arguments.pop("confirm", None)
            result = self.tools[name](**call_arguments)
            self.audit.record(name, tool_arguments, "ok", decision=decision)
            return result
        except Exception as exc:
            self.audit.record(name, tool_arguments, "error", decision=decision, error=str(exc))
            raise

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "description": description}
            for name, description in {
                "load_board_context": "Load and validate a board context file.",
                "instrument_status": "Report configured bench instrument backends.",
                "model_status": "Report configured model adapter backend.",
                "safety_status": "Report safety policy and audit log location.",
                "validate_session": "Validate the current or saved diagnostic session.",
                "read_audit_log": "Read recent JSONL audit events.",
                "plan_initial_measurements": "Plan a low-risk first measurement sequence.",
                "list_nets": "List nets with optional domain/risk filters.",
                "trace_net_neighbors": "Trace component, rail and test point neighbors for a net.",
                "find_test_points": "Find test points by net, measurement kind or risk level.",
                "trace_power_path": "Trace upstream rails feeding a target net.",
                "list_downstream_loads": "List nearby downstream load components for a rail or net.",
                "set_power_rail": "Safety-check and mock a programmable PSU rail action.",
                "measure_dc_voltage": "Measure DC voltage on a net with the configured DMM.",
                "measure_impedance": "Measure power-off impedance on a net with the configured DMM.",
                "capture_waveform": "Capture a synthetic mock waveform and write a CSV artifact.",
                "capture_logic": "Capture a digital logic trace and write a CSV artifact.",
                "capture_scope_screenshot": "Capture a scope screenshot artifact for a net.",
                "extract_signal_features": "Extract basic voltage features from a waveform CSV.",
                "diagnose_hardware": "Run rule-based diagnosis over current session measurements.",
                "suggest_next_probe": "Suggest low-risk next measurements.",
                "esp32_set_mux": "Mock the ESP32 fixture MUX tool.",
                "esp32_reset_dut": "Mock the ESP32 fixture DUT reset tool.",
            }.items()
        ]

    def read_resource(self, uri: str) -> dict[str, Any]:
        board = self.require_board()
        if uri == f"board://context/{board.board_id}":
            return {"ok": True, "content": board.data}
        if uri == f"board://topology/{board.board_id}":
            return {
                "ok": True,
                "content": {
                    "nets": self.require_topology().list_nets(),
                    "rails": list(board.rails.values()),
                },
            }
        if uri.startswith(f"board://net/{board.board_id}/"):
            net = uri.rsplit("/", 1)[-1]
            return self.trace_net_neighbors(net, depth=1)
        session = self.require_session()
        if uri == f"session://measurements/{session.session_id}":
            return {"ok": True, "content": session.data}
        artifact_prefix = f"session://artifacts/{session.session_id}/"
        if uri.startswith(artifact_prefix):
            artifact_id = uri.removeprefix(artifact_prefix)
            artifact = next((item for item in session.data["artifacts"] if item.get("id") == artifact_id), None)
            if artifact is None:
                raise ValueError(f"Unknown artifact resource: {artifact_id}")
            artifact_path = Path(str(artifact.get("uri", "")))
            if (
                artifact.get("mime_type", "").startswith("text/")
                or artifact.get("mime_type") == "image/svg+xml"
            ) and artifact_path.exists():
                return {
                    "ok": True,
                    "mime_type": artifact.get("mime_type", "text/plain"),
                    "text": artifact_path.read_text(encoding="utf-8"),
                    "metadata": artifact,
                }
            return {
                "ok": True,
                "mime_type": "application/json",
                "content": {
                    "artifact": artifact,
                    "available_as_text": False,
                    "reason": "Only text artifacts are returned inline by the bench MCP prototype.",
                },
            }
        raise ValueError(f"Unknown resource URI: {uri}")

    def save_session(self, path: str | Path) -> dict[str, Any]:
        session = self.require_session()
        target = session.save(path)
        validation = validate_session_file(target, check_artifacts=True)
        return {"ok": validation["ok"], "path": str(target), "validation_errors": validation["errors"]}

    def demo(self, board_path: str | Path, symptom: str, output_session: str | Path | None = None) -> dict[str, Any]:
        loaded = self.load_board_context_tool(str(board_path), observed_symptom=symptom)
        board = self.require_board()
        first_rail = next(iter(board.rails.values()), None)
        if first_rail:
            rail_limit = first_rail.get("current_limit") or {"max": 0.2}
            self.call_tool(
                "set_power_rail",
                {
                    "rail": first_rail["name"],
                    "voltage_V": float(first_rail.get("nominal_voltage", 5.0)),
                    "current_limit_A": min(float(rail_limit.get("max", 0.2)), 0.18),
                    "output": True,
                    "dry_run": True,
                },
            )
        preferred = "VOUT_3V3" if "VOUT_3V3" in board.nets else next(iter(board.nets))
        waveform = self.call_tool("capture_waveform", {"net": preferred, "sample_count": 512, "duration_s": 0.02})
        diagnosis = self.call_tool("diagnose_hardware", {})
        saved = None
        if output_session:
            saved = self.save_session(output_session)
        return {
            "ok": True,
            "loaded": loaded,
            "waveform": waveform,
            "diagnosis": diagnosis,
            "session_path": saved["path"] if saved else None,
        }

    def _validate_power_request(self, rail_info: dict[str, Any], voltage_v: float, current_limit_a: float) -> None:
        if voltage_v < 0:
            raise ValueError("voltage_V must be non-negative")
        if current_limit_a <= 0:
            raise ValueError("current_limit_A must be positive")
        nominal = float(rail_info.get("nominal_voltage", voltage_v))
        if voltage_v > nominal * 1.2 + 0.05:
            raise ValueError(f"Requested {voltage_v} V exceeds safe margin for rail {rail_info['name']}")
        limit = rail_info.get("current_limit")
        if isinstance(limit, dict) and current_limit_a > float(limit.get("max", current_limit_a)):
            raise ValueError(f"Requested current limit exceeds rail max for {rail_info['name']}")

    def _resolve_test_point(
        self,
        net: str,
        test_point: str | None,
        measurement: str,
        required: bool = True,
    ) -> dict[str, Any] | None:
        board = self.require_board()
        if test_point:
            point = board.test_points.get(test_point)
            if point is None:
                raise ValueError(f"Unknown test point: {test_point}")
            if point["net"] != net:
                raise ValueError(f"Test point {test_point} is on {point['net']}, not {net}")
            return point
        for point in board.test_points.values():
            allowed = point.get("allowed_measurements") or []
            if point["net"] == net and (measurement in allowed or not allowed):
                return point
        if required:
            raise ValueError(f"No test point found for {net} supporting {measurement}")
        return None

    def _diagnose_from_session(
        self,
        board: BoardContext,
        session: DiagnosticSession,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        evidence: list[str] = []
        related_nets: set[str] = set()
        low_voltage_rails: list[str] = []
        current_limited = False
        for measurement in session.data["measurements"]:
            target_net = measurement.get("target", {}).get("net")
            if target_net in board.nets:
                related_nets.add(target_net)
            features = measurement.get("features", {})
            if features.get("current_limited") or measurement.get("result", {}).get("current_limited"):
                current_limited = True
                evidence.append("Input rail reached the configured current limit.")
            if measurement.get("kind") == "waveform" and target_net in board.nets:
                expected = board.nets[target_net].get("expected_voltage")
                if expected and features.get("v_max_V") is not None and features["v_max_V"] < expected["min"]:
                    low_voltage_rails.append(target_net)
                    evidence.append(
                        f"{target_net} waveform peaks at {features['v_max_V']} V, below expected {expected['min']} V."
                    )
        if low_voltage_rails and current_limited:
            summary = f"{low_voltage_rails[0]} likely collapses because the upstream rail is current-limited."
            confidence = 0.72
            severity = "fault"
        elif low_voltage_rails:
            summary = f"{low_voltage_rails[0]} is below its expected voltage range."
            confidence = 0.58
            severity = "warning"
        else:
            summary = "No hard fault was identified from the available mock measurements."
            confidence = 0.35
            severity = "info"
            evidence.append("Available measurements remain within broad mock limits.")
        action_net = "SW_NODE" if "SW_NODE" in board.nets and low_voltage_rails else next(iter(board.nets))
        point = self._resolve_test_point(action_net, None, "waveform", required=False)
        action = {
            "type": "measure_net",
            "net": action_net,
            "test_point": point.get("id") if point else None,
            "instrument_kind": "oscilloscope",
            "reason": "Check converter switching behavior before changing power conditions.",
            "risk_level": board.nets[action_net].get("risk_level", "low"),
            "requires_confirmation": board.nets[action_net].get("risk_level") == "high",
        }
        finding = {
            "id": f"finding_{len(session.data['findings']) + 1:03d}",
            "timestamp": utc_now(),
            "summary": summary,
            "confidence": confidence,
            "severity": severity,
            "evidence": evidence,
            "related_nets": sorted(related_nets | set(low_voltage_rails) | {action_net}),
            "related_components": [item["designator"] for item in self.require_topology().components_on_net(action_net)],
        }
        return finding, [action]

    def _next_measurement_id(self) -> str:
        session = self.require_session()
        return f"m{len(session.data['measurements']) + 1:03d}"

    def _sync_session_instruments(self) -> None:
        if self.session is None:
            return
        self.session.data["instruments"] = [
            self.psu.status(),
            self.scope.status(),
            self.dmm.status(),
            self.logic.status(),
            {"id": "mock_fixture", "kind": "esp32_fixture", "backend": "mock"},
        ]


def _timestamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _measurement_target(net: str, point: dict[str, Any] | None) -> dict[str, Any]:
    target: dict[str, Any] = {"net": net}
    if point:
        target["test_point"] = point["id"]
    return target


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _conservative_current_limit(rail: dict[str, Any]) -> float:
    limit = rail.get("current_limit")
    if not isinstance(limit, dict):
        return 0.05
    minimum = float(limit.get("min", 0.01))
    maximum = float(limit.get("max", 0.05))
    return round(max(minimum, min(maximum, 0.1)), 6)


def _load_instrument_config(config: dict[str, Any] | str | Path | None) -> dict[str, Any]:
    return _load_json_config(config, "instrument config")


def _load_json_config(config: dict[str, Any] | str | Path | None, label: str) -> dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, dict):
        return config
    path = Path(config)
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return loaded


def to_mcp_tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ],
        "isError": not payload.get("ok", False),
    }

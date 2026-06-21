from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request

from ai_hardware_bench.bench import BenchApp
from ai_hardware_bench.data import BoardContext, load_board_context
from ai_hardware_bench.importers import import_board
from ai_hardware_bench.mcp_server import StdioJsonRpcServer
from ai_hardware_bench.model import ModelOutputValidationError
from ai_hardware_bench.regression import run_regression_suite
from ai_hardware_bench.report import generate_session_report
from ai_hardware_bench.session import validate_session
from ai_hardware_bench.web import create_console_server


ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "examples" / "boards" / "usb_power_stage.yaml"


class BenchPrototypeTest(unittest.TestCase):
    def test_load_yaml_board_without_external_dependencies(self) -> None:
        board = load_board_context(BOARD)
        self.assertEqual(board.board_id, "usb_power_stage_demo")
        self.assertIn("VOUT_3V3", board.nets)
        self.assertEqual(board.canonical_net("3V3"), "VOUT_3V3")

    def test_board_context_rejects_bad_cross_references(self) -> None:
        board = load_board_context(BOARD)

        bad_enable = json.loads(json.dumps(board.data))
        bad_enable["rails"][1]["enable_net"] = "MISSING_ENABLE"
        with self.assertRaisesRegex(ValueError, "unknown enable_net MISSING_ENABLE"):
            BoardContext(bad_enable)

        duplicate_test_point = json.loads(json.dumps(board.data))
        duplicate_test_point["test_points"].append(dict(duplicate_test_point["test_points"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate test point id"):
            BoardContext(duplicate_test_point)

        bad_constraint = json.loads(json.dumps(board.data))
        bad_constraint["constraints"].append(
            {
                "id": "bad_manual_target",
                "type": "manual_confirmation",
                "targets": ["MISSING_NET"],
                "description": "Invalid target should be rejected.",
            }
        )
        with self.assertRaisesRegex(ValueError, "unknown net target MISSING_NET"):
            BoardContext(bad_constraint)

    def test_mock_diagnostic_flow_writes_session_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            app = BenchApp(Path(tmp) / "artifacts")
            result = app.demo(BOARD, "3V3 rail does not stay up after USB input is applied.", session_path)
            self.assertTrue(result["ok"])
            self.assertTrue(session_path.exists())
            validation = app.validate_session_tool(str(session_path))
            self.assertTrue(validation["ok"], validation["errors"])
            session = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(session["board_id"], "usb_power_stage_demo")
            self.assertGreaterEqual(len(session["measurements"]), 2)
            self.assertEqual(session["findings"][0]["severity"], "warning")
            artifact_path = Path(session["artifacts"][0]["uri"])
            self.assertTrue(artifact_path.exists())
            audit = app.read_audit_log()
            self.assertGreaterEqual(audit["count"], 2)

    def test_session_validation_rejects_schema_shape_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            app = BenchApp(Path(tmp) / "artifacts")
            app.demo(BOARD, "3V3 rail does not stay up after USB input is applied.", session_path)
            session = json.loads(session_path.read_text(encoding="utf-8"))

            session["instruments"][0]["kind"] = "magic_probe"
            session["measurements"][0]["kind"] = "bad_kind"
            session["measurements"][0]["instrument_id"] = "missing_instrument"
            session["measurements"][0]["artifact_ids"] = "not-a-list"
            session["findings"][0]["severity"] = "maybe"
            session["next_actions"][0]["type"] = "teleport_probe"
            session["next_actions"][0]["risk_level"] = "extreme"
            session["next_actions"][0]["requires_confirmation"] = "yes"
            session["artifacts"][0]["kind"] = "bad_artifact"
            session["artifacts"][0]["sha256"] = "not-a-sha"
            session["artifacts"].append(dict(session["artifacts"][0]))

            errors = validate_session(session, check_artifacts=False)
            joined = "\n".join(errors)
            self.assertIn("instrument[0].kind is not supported: magic_probe", joined)
            self.assertIn("measurement[0].kind is not supported: bad_kind", joined)
            self.assertIn("measurement[0].instrument_id references unknown instrument missing_instrument", joined)
            self.assertIn("measurement[0].artifact_ids must be a list of strings", joined)
            self.assertIn("finding[0].severity is not supported: maybe", joined)
            self.assertIn("next_actions[0].type is not supported: teleport_probe", joined)
            self.assertIn("next_actions[0].risk_level is not supported: extreme", joined)
            self.assertIn("next_actions[0].requires_confirmation must be boolean", joined)
            self.assertIn("artifact[0].kind is not supported: bad_artifact", joined)
            self.assertIn("artifact[0].sha256 must be a 64-character hex string", joined)
            self.assertIn("Duplicate artifact id:", joined)

    def test_session_validation_checks_board_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            app = BenchApp(Path(tmp) / "artifacts")
            app.demo(BOARD, "3V3 rail does not stay up after USB input is applied.", session_path)
            session = json.loads(session_path.read_text(encoding="utf-8"))
            board = load_board_context(BOARD)

            session["board_id"] = "wrong_board"
            session["measurements"][0]["target"]["net"] = "VOUT_3V3"
            session["measurements"][0]["target"]["test_point"] = "TP1"
            session["measurements"][0]["target"]["component"] = "MISSING_COMPONENT"
            session["measurements"][0]["target"]["pin"] = "1"
            session["findings"][0]["related_nets"] = ["MISSING_NET"]
            session["findings"][0]["related_components"] = ["MISSING_COMPONENT"]
            session["next_actions"] = [
                {
                    "type": "measure_net",
                    "net": "SW_NODE",
                    "test_point": "TP3",
                    "component": "MISSING_COMPONENT",
                    "reason": "Probe a high-risk net without confirmation.",
                    "risk_level": "high",
                    "requires_confirmation": False,
                }
            ]

            errors = validate_session(session, check_artifacts=False, board=board)
            joined = "\n".join(errors)
            self.assertIn("session board_id wrong_board does not match board context usb_power_stage_demo", joined)
            self.assertIn("measurement[0].target.test_point TP1 is not on net VOUT_3V3", joined)
            self.assertIn("measurement[0].target.component references unknown component MISSING_COMPONENT", joined)
            self.assertIn("finding[0].related_nets references unknown net MISSING_NET", joined)
            self.assertIn("finding[0].related_components references unknown component MISSING_COMPONENT", joined)
            self.assertIn("next_actions[0].test_point TP3 is not on net SW_NODE", joined)
            self.assertIn("next_actions[0].component references unknown component MISSING_COMPONENT", joined)
            self.assertIn("next_actions[0] targets high-risk net SW_NODE without requires_confirmation=true", joined)

    def test_power_safety_rejects_overcurrent(self) -> None:
        app = BenchApp()
        app.load_board_context_tool(str(BOARD))
        with self.assertRaises(ValueError):
            app.set_power_rail("USB_5V", voltage_V=5.0, current_limit_A=2.0)

    def test_mcp_tools_call_shape(self) -> None:
        app = BenchApp()
        server = StdioJsonRpcServer(app)
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "load_board_context",
                    "arguments": {"path": str(BOARD), "observed_symptom": "3V3 rail does not stay up"},
                },
            }
        )
        self.assertIsNotNone(response)
        self.assertEqual(response["id"], 1)
        text = response["result"]["content"][0]["text"]
        payload = json.loads(text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["board_id"], "usb_power_stage_demo")

    def test_mcp_tools_list_exposes_input_schemas(self) -> None:
        app = BenchApp()
        server = StdioJsonRpcServer(app)
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertIsNotNone(response)
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        self.assertEqual(set(tools), set(app.tools))
        for tool in tools.values():
            schema = tool.get("inputSchema")
            self.assertIsInstance(schema, dict)
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)
            self.assertIn("required", schema)
            self.assertFalse(schema["additionalProperties"])

        load_schema = tools["load_board_context"]["inputSchema"]
        self.assertEqual(load_schema["required"], ["path"])
        self.assertEqual(load_schema["properties"]["path"]["type"], "string")

        waveform_schema = tools["capture_waveform"]["inputSchema"]
        self.assertEqual(waveform_schema["required"], ["net"])
        self.assertIn("confirm", waveform_schema["properties"])
        self.assertEqual(waveform_schema["properties"]["sample_count"]["minimum"], 1)

        mux_schema = tools["esp32_set_mux"]["inputSchema"]
        self.assertEqual(mux_schema["properties"]["channel"]["maximum"], 31)
        self.assertIn("dry_run", mux_schema["properties"])

    def test_tool_calls_validate_input_schema_before_execution(self) -> None:
        app = BenchApp()
        app.load_board_context_tool(str(BOARD))
        with self.assertRaisesRegex(ValueError, "capture_waveform.net is required"):
            app.call_tool("capture_waveform", {"sample_count": 32})
        with self.assertRaisesRegex(ValueError, "capture_waveform.sample_count must be >= 1"):
            app.call_tool("capture_waveform", {"net": "VOUT_3V3", "sample_count": 0})
        with self.assertRaisesRegex(ValueError, "list_nets.domain must be one of"):
            app.call_tool("list_nets", {"domain": "bad_domain"})
        with self.assertRaisesRegex(ValueError, "measure_dc_voltage.extra is not a supported argument"):
            app.call_tool("measure_dc_voltage", {"net": "VOUT_3V3", "extra": True})

        with self.assertRaises(PermissionError):
            app.call_tool("capture_waveform", {"net": "SW_NODE", "extra": True})
        audit = app.read_audit_log()
        self.assertTrue(audit["events"][-1]["safety"]["requires_confirmation"])

    def test_mcp_prompts_list_and_get(self) -> None:
        app = BenchApp()
        app.demo(BOARD, "3V3 rail does not stay up after USB input is applied.")
        server = StdioJsonRpcServer(app)
        listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "prompts/list", "params": {}})
        self.assertIsNotNone(listed)
        prompts = {item["name"]: item for item in listed["result"]["prompts"]}
        prompt_names = set(prompts)
        self.assertIn("diagnose_power_rail", prompt_names)
        rail_schema = prompts["diagnose_power_rail"]["inputSchema"]
        self.assertEqual(rail_schema["type"], "object")
        self.assertFalse(rail_schema["additionalProperties"])
        self.assertEqual(rail_schema["properties"]["rail"]["type"], ["string", "null"])

        fetched = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "prompts/get",
                "params": {"name": "diagnose_power_rail", "arguments": {"rail": "3V3_BUCK"}},
            }
        )
        self.assertIsNotNone(fetched)
        message = fetched["result"]["messages"][0]["content"]["text"]
        self.assertIn("3V3_BUCK", message)
        self.assertIn("USB Power Stage Demo", message)
        self.assertIn("Relevant measurements", message)

        bad_prompt = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "prompts/get",
                "params": {"name": "plan_next_measurement", "arguments": {"focus_net": 42}},
            }
        )
        self.assertIsNotNone(bad_prompt)
        self.assertIn("error", bad_prompt)
        self.assertIn("plan_next_measurement.focus_net must be", bad_prompt["error"]["message"])

        unknown_arg = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "prompts/get",
                "params": {"name": "diagnose_boot_sequence", "arguments": {"extra": "nope"}},
            }
        )
        self.assertIsNotNone(unknown_arg)
        self.assertIn("diagnose_boot_sequence.extra is not a supported argument", unknown_arg["error"]["message"])

    def test_mcp_resources_include_nets_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.demo(BOARD, "3V3 rail does not stay up after USB input is applied.")
            server = StdioJsonRpcServer(app)
            templates = server.handle({"jsonrpc": "2.0", "id": 0, "method": "resources/templates/list", "params": {}})
            self.assertIsNotNone(templates)
            uri_templates = {item["uriTemplate"] for item in templates["result"]["resourceTemplates"]}
            self.assertIn("board://net/{board_id}/{net_name}", uri_templates)
            self.assertIn("session://artifacts/{session_id}/{artifact_id}", uri_templates)

            listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}})
            self.assertIsNotNone(listed)
            uris = {item["uri"] for item in listed["result"]["resources"]}
            self.assertIn("board://net/usb_power_stage_demo/VOUT_3V3", uris)
            artifact_id = app.session.data["artifacts"][0]["id"]
            artifact_uri = f"session://artifacts/{app.session.session_id}/{artifact_id}"
            self.assertIn(artifact_uri, uris)

            read_artifact = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "resources/read",
                    "params": {"uri": artifact_uri},
                }
            )
            self.assertIsNotNone(read_artifact)
            content = read_artifact["result"]["contents"][0]
            self.assertEqual(content["mimeType"], "text/csv")
            self.assertIn("t_s,voltage_V", content["text"])

    def test_scope_screenshot_artifact_report_and_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            report_path = Path(tmp) / "report.html"
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="capture screenshot")
            waveform = app.call_tool("capture_waveform", {"net": "VOUT_3V3", "sample_count": 64})
            screenshot = app.call_tool(
                "capture_scope_screenshot",
                {"net": "VOUT_3V3", "artifact_id": waveform["artifact"]["id"]},
            )
            self.assertTrue(screenshot["ok"])
            self.assertEqual(screenshot["artifact"]["kind"], "scope_screenshot")
            self.assertEqual(screenshot["artifact"]["mime_type"], "image/svg+xml")
            self.assertTrue(Path(screenshot["artifact"]["uri"]).exists())

            saved = app.save_session(session_path)
            self.assertTrue(saved["ok"], saved["validation_errors"])
            report = generate_session_report(session_path, report_path, audit_path=Path(tmp) / "artifacts" / "audit.jsonl")
            self.assertTrue(report["ok"])
            html = report_path.read_text(encoding="utf-8")
            self.assertIn("scope_screenshot", html)
            self.assertIn("data:image/svg+xml;base64", html)

            server = StdioJsonRpcServer(app)
            artifact_uri = f"session://artifacts/{app.session.session_id}/{screenshot['artifact']['id']}"
            read_artifact = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "resources/read",
                    "params": {"uri": artifact_uri},
                }
            )
            self.assertIsNotNone(read_artifact)
            content = read_artifact["result"]["contents"][0]
            self.assertEqual(content["mimeType"], "image/svg+xml")
            self.assertIn("<svg", content["text"])

    def test_instrument_status_defaults_to_mock(self) -> None:
        app = BenchApp()
        status = app.instrument_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["instruments"][0]["backend"], "mock")
        self.assertEqual(status["instruments"][1]["backend"], "mock")
        self.assertEqual(status["instruments"][2]["kind"], "dmm")
        self.assertEqual(status["instruments"][2]["backend"], "mock")
        self.assertEqual(status["instruments"][3]["kind"], "logic_analyzer")
        self.assertEqual(status["instruments"][3]["backend"], "mock")

    def test_plan_initial_measurements_writes_low_risk_actions(self) -> None:
        app = BenchApp()
        app.load_board_context_tool(str(BOARD))
        result = app.call_tool("plan_initial_measurements", {"max_actions": 10})
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["count"], 4)
        self.assertEqual(app.session.data["next_actions"], result["next_actions"])
        self.assertEqual(result["next_actions"][0]["measurement_kind"], "impedance")
        action_nets = {action.get("net") for action in result["next_actions"]}
        self.assertIn("VOUT_3V3", action_nets)
        self.assertIn("VDD_1V8", action_nets)
        self.assertIn("PG_3V3", action_nets)
        self.assertNotIn("SW_NODE", action_nets)
        self.assertTrue(all(action["risk_level"] in {"low", "medium"} for action in result["next_actions"]))

    def test_dmm_measurements_write_valid_session_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="3V3 rail does not stay up")
            impedance = app.call_tool("measure_impedance", {"net": "VOUT_3V3"})
            voltage = app.call_tool("measure_dc_voltage", {"net": "VOUT_3V3"})
            self.assertEqual(impedance["measurement"]["kind"], "impedance")
            self.assertEqual(impedance["measurement"]["target"]["test_point"], "TP3")
            self.assertFalse(impedance["measurement"]["features"]["short_to_ground"])
            self.assertEqual(voltage["measurement"]["kind"], "dc_voltage")
            self.assertTrue(voltage["measurement"]["features"]["below_expected"])
            self.assertIn("mock_dmm", {instrument["id"] for instrument in app.session.data["instruments"]})
            validation = app.validate_session_tool()
            self.assertTrue(validation["ok"], validation["errors"])

    def test_dmm_plan_actions_are_executable(self) -> None:
        app = BenchApp()
        app.load_board_context_tool(str(BOARD))
        plan = app.call_tool("plan_initial_measurements", {"max_actions": 6})
        dmm_actions = [
            action
            for action in plan["next_actions"]
            if action.get("instrument_kind") == "dmm" and action.get("test_point")
        ]
        self.assertGreaterEqual(len(dmm_actions), 2)
        for action in dmm_actions[:2]:
            tool = "measure_impedance" if action.get("measurement_kind") == "impedance" else "measure_dc_voltage"
            result = app.call_tool(tool, {"net": action["net"], "test_point": action["test_point"]})
            self.assertTrue(result["ok"])
            self.assertEqual(result["measurement"]["target"]["net"], action["net"])

    def test_impedance_measurement_requires_power_off(self) -> None:
        app = BenchApp()
        app.load_board_context_tool(str(BOARD))
        with self.assertRaises(PermissionError):
            app.call_tool("measure_impedance", {"net": "VOUT_3V3", "power_state": "on"})
        audit = app.read_audit_log()
        self.assertEqual(audit["events"][-1]["outcome"], "error")
        self.assertTrue(audit["events"][-1]["safety"]["requires_confirmation"])

    def test_logic_capture_writes_artifact_and_valid_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="PG_3V3 power good low remains deasserted")
            result = app.call_tool("capture_logic", {"net": "PG_3V3", "sample_count": 32})
            self.assertTrue(result["ok"])
            self.assertEqual(result["measurement"]["kind"], "logic")
            self.assertEqual(result["measurement"]["target"]["test_point"], "TP4")
            self.assertTrue(result["measurement"]["features"]["stuck_low"])
            self.assertEqual(result["artifact"]["kind"], "logic_csv")
            self.assertTrue(Path(result["artifact"]["uri"]).exists())
            validation = app.validate_session_tool()
            self.assertTrue(validation["ok"], validation["errors"])

    def test_topology_tools_find_power_path_and_test_points(self) -> None:
        app = BenchApp()
        app.load_board_context_tool(str(BOARD))
        points = app.call_tool("find_test_points", {"net": "VOUT_3V3", "measurement": "waveform"})
        self.assertTrue(points["ok"])
        self.assertEqual(points["test_points"][0]["id"], "TP3")

        path = app.call_tool("trace_power_path", {"net": "VOUT_3V3"})
        self.assertTrue(path["ok"])
        self.assertEqual(path["paths"][0]["rails"][-1]["name"], "3V3_BUCK")

        loads = app.call_tool("list_downstream_loads", {"rail": "3V3_BUCK", "depth": 1})
        self.assertTrue(loads["ok"])
        designators = {load["component"]["designator"] for load in loads["loads"]}
        self.assertIn("U1", designators)

    def test_model_status_defaults_to_rules(self) -> None:
        app = BenchApp()
        status = app.model_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["model"]["backend"], "rules")

    def test_rule_model_detects_overvoltage_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="3V3 rail overvoltage exceeds the configured maximum")
            app.call_tool("capture_waveform", {"net": "VOUT_3V3", "sample_count": 64})
            result = app.call_tool("diagnose_hardware", {})
            self.assertEqual(result["finding"]["severity"], "critical")
            self.assertIn("above", result["finding"]["summary"])
            self.assertEqual(result["next_actions"][0]["type"], "stop")

    def test_rule_model_detects_excessive_ripple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="3V3 rail ripple appears excessive")
            app.call_tool("capture_waveform", {"net": "VOUT_3V3", "sample_count": 128})
            result = app.call_tool("diagnose_hardware", {})
            self.assertEqual(result["finding"]["severity"], "warning")
            self.assertIn("ripple", result["finding"]["summary"])
            self.assertEqual(result["next_actions"][0]["net"], "SW_NODE")

    def test_rule_model_detects_enable_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="3V3 rail disabled because EN_3V3 enable low")
            voltage = app.call_tool("measure_dc_voltage", {"net": "EN_3V3"})
            self.assertEqual(voltage["measurement"]["target"], {"net": "EN_3V3"})
            result = app.call_tool("diagnose_hardware", {})
            self.assertEqual(result["finding"]["severity"], "fault")
            self.assertIn("EN_3V3", result["finding"]["summary"])
            self.assertEqual(result["next_actions"][0]["type"], "inspect_component")
            self.assertEqual(result["next_actions"][0]["net"], "EN_3V3")

    def test_rule_model_detects_power_good_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="3V3 rail is present but PG_3V3 power good low")
            app.call_tool("measure_dc_voltage", {"net": "VOUT_3V3"})
            app.call_tool("capture_logic", {"net": "PG_3V3", "sample_count": 32})
            result = app.call_tool("diagnose_hardware", {})
            self.assertEqual(result["finding"]["severity"], "fault")
            self.assertIn("PG_3V3", result["finding"]["summary"])
            self.assertEqual(result["next_actions"][0]["type"], "inspect_component")
            self.assertEqual(result["next_actions"][0]["net"], "PG_3V3")

    def test_rule_model_detects_power_off_short(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="3V3 rail appears shorted to ground")
            impedance = app.call_tool("measure_impedance", {"net": "VOUT_3V3"})
            self.assertTrue(impedance["measurement"]["features"]["short_to_ground"])
            result = app.call_tool("diagnose_hardware", {})
            self.assertEqual(result["finding"]["severity"], "critical")
            self.assertIn("shorted", result["finding"]["summary"])
            self.assertEqual(result["next_actions"][0]["type"], "stop")

    def test_rule_model_detects_inactive_switch_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="SW_NODE not switching with no pulses")
            waveform = app.call_tool("capture_waveform", {"net": "SW_NODE", "confirm": True, "sample_count": 64})
            self.assertLess(waveform["measurement"]["features"]["v_pp_V"], 0.2)
            result = app.call_tool("diagnose_hardware", {})
            self.assertEqual(result["finding"]["severity"], "fault")
            self.assertIn("SW_NODE", result["finding"]["summary"])
            self.assertEqual(result["next_actions"][0]["type"], "inspect_component")
            self.assertEqual(result["next_actions"][0]["net"], "SW_NODE")
            self.assertTrue(result["next_actions"][0]["requires_confirmation"])

    def test_rule_model_detects_reset_stuck_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="MCU_RESET_N reset stuck low and not released")
            logic = app.call_tool("capture_logic", {"net": "MCU_RESET_N", "sample_count": 32})
            self.assertEqual(logic["measurement"]["target"]["test_point"], "TP5")
            self.assertTrue(logic["measurement"]["features"]["stuck_low"])
            result = app.call_tool("diagnose_hardware", {})
            self.assertEqual(result["finding"]["severity"], "fault")
            self.assertIn("MCU_RESET_N", result["finding"]["summary"])
            self.assertEqual(result["next_actions"][0]["type"], "inspect_component")
            self.assertEqual(result["next_actions"][0]["net"], "MCU_RESET_N")

    def test_rule_model_detects_missing_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="MCU_CLK clock missing and crystal not oscillating")
            logic = app.call_tool("capture_logic", {"net": "MCU_CLK", "sample_count": 32})
            self.assertEqual(logic["measurement"]["target"]["test_point"], "TP6")
            self.assertTrue(logic["measurement"]["features"]["stuck_low"])
            self.assertEqual(logic["measurement"]["features"]["transition_count"], 0)
            result = app.call_tool("diagnose_hardware", {})
            self.assertEqual(result["finding"]["severity"], "fault")
            self.assertIn("MCU_CLK", result["finding"]["summary"])
            self.assertEqual(result["next_actions"][0]["type"], "inspect_component")
            self.assertEqual(result["next_actions"][0]["net"], "MCU_CLK")

    def test_rule_model_detects_i2c_bus_held_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD), observed_symptom="I2C bus held low on I2C_SCL by an attached device")
            logic = app.call_tool("capture_logic", {"net": "I2C_SCL", "sample_count": 32})
            self.assertEqual(logic["measurement"]["target"]["test_point"], "TP7")
            self.assertTrue(logic["measurement"]["features"]["stuck_low"])
            result = app.call_tool("diagnose_hardware", {})
            self.assertEqual(result["finding"]["severity"], "fault")
            self.assertIn("I2C_SCL", result["finding"]["summary"])
            self.assertEqual(result["next_actions"][0]["type"], "inspect_component")
            self.assertEqual(result["next_actions"][0]["net"], "I2C_SCL")

    def test_rule_model_detects_ldo_output_abnormal_with_input_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(
                str(BOARD),
                observed_symptom="LDO input is normal but 1V8 LDO output abnormal and 1V8 low",
            )
            input_voltage = app.call_tool("measure_dc_voltage", {"net": "VOUT_3V3"})
            output_voltage = app.call_tool("measure_dc_voltage", {"net": "VDD_1V8"})
            self.assertTrue(input_voltage["measurement"]["features"]["within_expected"])
            self.assertTrue(output_voltage["measurement"]["features"]["below_expected"])
            self.assertEqual(output_voltage["measurement"]["target"]["test_point"], "TP9")
            result = app.call_tool("diagnose_hardware", {})
            self.assertEqual(result["finding"]["severity"], "fault")
            self.assertIn("VDD_1V8", result["finding"]["summary"])
            self.assertEqual(result["next_actions"][0]["type"], "inspect_component")
            self.assertEqual(result["next_actions"][0]["net"], "VDD_1V8")
            self.assertEqual(result["next_actions"][0]["component"], "U4")

    def test_json_http_model_output_is_validated_and_saved(self) -> None:
        payload = {
            "finding": {
                "id": "finding_http_001",
                "timestamp": "2026-06-15T00:00:00Z",
                "summary": "HTTP model identified no hard fault.",
                "confidence": 0.44,
                "severity": "info",
                "evidence": ["Synthetic model response was accepted."],
                "related_nets": ["VOUT_3V3"],
                "related_components": ["U1"],
            },
            "next_actions": [
                {
                    "type": "measure_net",
                    "net": "VOUT_3V3",
                    "test_point": "TP3",
                    "instrument_kind": "oscilloscope",
                    "reason": "Capture the rail waveform before changing conditions.",
                    "risk_level": "low",
                    "requires_confirmation": False,
                }
            ],
        }
        server, thread, endpoint = start_json_model_server(payload)
        try:
            app = BenchApp(model_config={"backend": "json_http", "endpoint": endpoint})
            app.load_board_context_tool(str(BOARD))
            result = app.call_tool("diagnose_hardware", {})
            self.assertTrue(result["ok"])
            self.assertEqual(app.session.data["findings"][0]["id"], "finding_http_001")
            self.assertEqual(app.session.data["next_actions"][0]["test_point"], "TP3")
        finally:
            stop_server(server, thread)

    def test_json_http_model_rejects_invalid_output(self) -> None:
        payload = {
            "finding": {
                "id": "finding_bad",
                "timestamp": "2026-06-15T00:00:00Z",
                "summary": "Bad model output.",
                "confidence": 2.0,
            },
            "next_actions": [
                {
                    "type": "measure_net",
                    "net": "SW_NODE",
                    "reason": "Missing confirmation on a high-risk net.",
                    "risk_level": "high",
                    "requires_confirmation": False,
                }
            ],
        }
        server, thread, endpoint = start_json_model_server(payload)
        try:
            app = BenchApp(model_config={"backend": "json_http", "endpoint": endpoint})
            app.load_board_context_tool(str(BOARD))
            with self.assertRaises(ModelOutputValidationError):
                app.call_tool("diagnose_hardware", {})
            self.assertEqual(app.session.data["findings"], [])
            audit = app.read_audit_log()
            self.assertEqual(audit["events"][-1]["outcome"], "error")
        finally:
            stop_server(server, thread)

    def test_high_risk_capture_requires_confirmation_and_audits_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD))
            with self.assertRaises(PermissionError):
                app.call_tool("capture_waveform", {"net": "SW_NODE"})
            audit = app.read_audit_log()
            self.assertEqual(audit["count"], 1)
            self.assertEqual(audit["events"][0]["outcome"], "error")
            self.assertTrue(audit["events"][0]["safety"]["requires_confirmation"])

    def test_confirmed_high_risk_capture_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = BenchApp(Path(tmp) / "artifacts")
            app.load_board_context_tool(str(BOARD))
            result = app.call_tool("capture_waveform", {"net": "SW_NODE", "confirm": True, "sample_count": 32})
            self.assertTrue(result["ok"])
            self.assertEqual(result["measurement"]["target"]["net"], "SW_NODE")

    def test_regression_suite_runs_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            suite = ROOT / "examples" / "regressions" / "usb_power_stage.json"
            result = run_regression_suite(suite, Path(tmp) / "regression")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["count"], 11)
            self.assertEqual(result["passed"], 11)

    def test_html_report_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            report_path = Path(tmp) / "report.html"
            app = BenchApp(Path(tmp) / "artifacts")
            app.demo(BOARD, "3V3 rail does not stay up after USB input is applied.", session_path)
            result = generate_session_report(session_path, report_path, audit_path=Path(tmp) / "artifacts" / "audit.jsonl")
            self.assertTrue(result["ok"])
            html = report_path.read_text(encoding="utf-8")
            self.assertIn("AI Hardware Report", html)
            self.assertIn("VOUT_3V3", html)
            self.assertIn("Waveform preview", html)
            self.assertIn("<polyline", html)

    def test_web_console_status_and_demo_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = create_console_server(
                host="127.0.0.1",
                port=0,
                board_path=BOARD,
                artifact_dir=Path(tmp) / "console",
                suite_path=ROOT / "examples" / "regressions" / "usb_power_stage.json",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                base = f"http://{host}:{port}"
                with urllib.request.urlopen(f"{base}/api/status", timeout=5) as response:
                    status = json.loads(response.read().decode("utf-8"))
                self.assertTrue(status["ok"])
                self.assertEqual(status["board"]["id"], "usb_power_stage_demo")

                with urllib.request.urlopen(f"{base}/api/board", timeout=5) as response:
                    board = json.loads(response.read().decode("utf-8"))
                self.assertTrue(board["ok"])
                self.assertIn("power_paths", board)
                self.assertEqual(board["nets"][0]["name"], "EN_3V3")

                plan_request = urllib.request.Request(
                    f"{base}/api/plan",
                    data=json.dumps({"max_actions": 5, "risk_ceiling": "medium"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(plan_request, timeout=5) as response:
                    plan = json.loads(response.read().decode("utf-8"))
                self.assertTrue(plan["ok"])
                self.assertGreaterEqual(plan["count"], 4)
                self.assertNotIn("SW_NODE", {action.get("net") for action in plan["next_actions"]})

                with urllib.request.urlopen(f"{base}/api/status", timeout=5) as response:
                    status_after_plan = json.loads(response.read().decode("utf-8"))
                self.assertEqual(status_after_plan["last_plan"]["count"], plan["count"])

                request = urllib.request.Request(
                    f"{base}/api/demo",
                    data=json.dumps({"symptom": "3V3 rail does not stay up"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    demo = json.loads(response.read().decode("utf-8"))
                self.assertTrue(demo["ok"])
                self.assertEqual(demo["report_url"], "/reports/demo/report.html")

                with urllib.request.urlopen(f"{base}/api/replay?max_points=40", timeout=5) as response:
                    replay = json.loads(response.read().decode("utf-8"))
                self.assertTrue(replay["ok"])
                self.assertEqual(replay["session_id"], demo["demo"]["loaded"]["session_id"])
                self.assertEqual(len(replay["waveforms"]), 1)
                waveform = replay["waveforms"][0]
                self.assertTrue(waveform["ok"])
                self.assertLessEqual(len(waveform["samples"]), 40)
                self.assertEqual(waveform["measurement"]["target"]["net"], "VOUT_3V3")

                import_request = urllib.request.Request(
                    f"{base}/api/import-board",
                    data=json.dumps(
                        {
                            "format": "csv",
                            "board_id": "console_csv_demo",
                            "name": "Console CSV Demo",
                            "content": (
                                "net_name,test_point,domain,risk_level,expected_voltage_min,expected_voltage_max,"
                                "allowed_measurements,component,pin,component_type\n"
                                "VIN,TP1,power,medium,4.75,5.25,\"dc_voltage,waveform\",J1,1,connector\n"
                                "GND,TP2,ground,low,,,dc_voltage,J1,2,connector\n"
                            ),
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(import_request, timeout=5) as response:
                    imported = json.loads(response.read().decode("utf-8"))
                self.assertTrue(imported["ok"], imported)
                self.assertEqual(imported["import"]["board_id"], "console_csv_demo")

                bom_import_request = urllib.request.Request(
                    f"{base}/api/import-board",
                    data=json.dumps(
                        {
                            "format": "bom",
                            "board_id": "console_bom_demo",
                            "name": "Console BOM Demo",
                            "content": "references,value,mpn,footprint\nU1,MCU,MCU-123,QFN32\n",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(bom_import_request, timeout=5) as response:
                    imported_bom = json.loads(response.read().decode("utf-8"))
                self.assertTrue(imported_bom["ok"], imported_bom)
                self.assertEqual(imported_bom["import"]["board_id"], "console_bom_demo")

                with urllib.request.urlopen(f"{base}/api/status", timeout=5) as response:
                    imported_status = json.loads(response.read().decode("utf-8"))
                self.assertEqual(imported_status["board"]["id"], "console_bom_demo")
                self.assertEqual(imported_status["last_import"]["board_id"], "console_bom_demo")

                with urllib.request.urlopen(f"{base}/api/board", timeout=5) as response:
                    imported_board = json.loads(response.read().decode("utf-8"))
                self.assertEqual(imported_board["board"]["id"], "console_bom_demo")
                self.assertEqual(imported_board["nets"][0]["name"], "UNASSIGNED")

                sch_import_request = urllib.request.Request(
                    f"{base}/api/import-board",
                    data=json.dumps(
                        {
                            "format": "kicad",
                            "board_id": "console_sch_demo",
                            "name": "Console SCH Demo",
                            "content": """
(kicad_sch
  (symbol
    (property "Reference" "U1")
    (property "Value" "MCU")
    (property "Footprint" "Package_QFN:QFN-32")
    (property "MPN" "MCU-123")
  )
)
""",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(sch_import_request, timeout=5) as response:
                    imported_sch = json.loads(response.read().decode("utf-8"))
                self.assertTrue(imported_sch["ok"], imported_sch)
                self.assertTrue(imported_sch["source_path"].endswith(".kicad_sch"))
                self.assertEqual(imported_sch["import"]["counts"]["components"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_import_testpoint_csv_to_board_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "points.csv"
            output = Path(tmp) / "board.json"
            source.write_text(
                "net_name,test_point,domain,risk_level,expected_voltage_min,expected_voltage_max,allowed_measurements,component,pin,component_type\n"
                "VIN,TP1,power,medium,4.75,5.25,\"dc_voltage,waveform\",J1,1,connector\n"
                "GND,TP2,ground,low,,,dc_voltage,J1,2,connector\n",
                encoding="utf-8",
            )
            result = import_board(source, "csv", "csv_demo", "CSV Demo", output)
            self.assertTrue(result["ok"])
            board = load_board_context(output)
            self.assertEqual(board.board_id, "csv_demo")
            self.assertIn("VIN", board.nets)
            self.assertEqual(board.test_points["TP1"]["net"], "VIN")

    def test_import_bom_csv_to_board_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bom.csv"
            output = Path(tmp) / "board.json"
            source.write_text(
                "references,value,mpn,footprint,type\n"
                "\"R1 R2\",10k,RC0603FR-0710KL,0603,resistor\n"
                "U1,MCU,MCU-123,QFN32,microcontroller\n",
                encoding="utf-8",
            )
            result = import_board(source, "bom", "bom_demo", "BOM Demo", output)
            self.assertTrue(result["ok"])
            board = load_board_context(output)
            self.assertEqual(board.board_id, "bom_demo")
            self.assertIn("UNASSIGNED", board.nets)
            self.assertEqual(board.components["R1"]["part_number"], "RC0603FR-0710KL")
            self.assertEqual(board.components["R2"]["package"], "0603")
            self.assertEqual(board.components["U1"]["type"], "microcontroller")

    def test_import_altium_csv_to_board_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "altium.csv"
            output = Path(tmp) / "board.json"
            source.write_text(
                "Net Name,Component Designator,Pin Designator,Comment,Footprint,Test Point\n"
                "VBUS,J1,1,USB-C,CONN,\n"
                "VBUS,TP1,1,TestPoint,TP,TP1\n"
                "GND,J1,2,USB-C,CONN,\n"
                "VOUT_3V3,U1,5,Regulator,SOT-23,\n",
                encoding="utf-8",
            )
            result = import_board(source, "altium", "altium_demo", "Altium Demo", output)
            self.assertTrue(result["ok"])
            board = load_board_context(output)
            self.assertEqual(board.board_id, "altium_demo")
            self.assertIn("VBUS", board.nets)
            self.assertEqual(board.nets["VBUS"]["domain"], "power")
            self.assertEqual(board.components["U1"]["package"], "SOT-23")
            self.assertEqual(board.components["J1"]["pins"][0]["net"], "VBUS")
            self.assertEqual(board.test_points["TP1"]["net"], "VBUS")

    def test_import_pick_place_csv_to_board_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "pick_place.csv"
            output = Path(tmp) / "board.json"
            source.write_text(
                "Designator,Comment,Footprint,Center-X(mm),Center-Y(mm),Rotation,Layer\n"
                "U1,Regulator,SOT-23,12.5,8.25,90,Top\n"
                "C1,10uF,0603,4.0,3.5,180,Bottom\n",
                encoding="utf-8",
            )
            result = import_board(source, "pnp", "pnp_demo", "PnP Demo", output)
            self.assertTrue(result["ok"])
            board = load_board_context(output)
            self.assertEqual(board.board_id, "pnp_demo")
            self.assertIn("UNASSIGNED", board.nets)
            self.assertEqual(board.components["U1"]["package"], "SOT-23")
            self.assertEqual(board.components["U1"]["placement"]["x"], 12.5)
            self.assertEqual(board.components["U1"]["placement"]["rotation_deg"], 90.0)
            self.assertEqual(board.components["C1"]["placement"]["side"], "Bottom")

    def test_import_kicad_xml_to_board_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "netlist.xml"
            output = Path(tmp) / "board.json"
            source.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<export>
  <components>
    <comp ref="U1"><value>Regulator</value><footprint>SOT-23</footprint></comp>
    <comp ref="TP1"><value>TestPoint</value></comp>
  </components>
  <nets>
    <net code="1" name="VIN"><node ref="U1" pin="1"/><node ref="TP1" pin="1"/></net>
    <net code="2" name="GND"><node ref="U1" pin="2"/></net>
  </nets>
</export>
""",
                encoding="utf-8",
            )
            result = import_board(source, "kicad", "kicad_demo", "KiCad Demo", output)
            self.assertTrue(result["ok"])
            board = load_board_context(output)
            self.assertEqual(board.board_id, "kicad_demo")
            self.assertIn("VIN", board.nets)
            self.assertEqual(board.test_points["TP1"]["net"], "VIN")

    def test_import_kicad_pcb_to_board_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "board.kicad_pcb"
            output = Path(tmp) / "board.json"
            source.write_text(
                """
(kicad_pcb
  (net 0 "")
  (net 1 "GND")
  (net 2 "VOUT_3V3")
  (footprint "Package_TO_SOT_SMD:SOT-23"
    (layer "F.Cu")
    (at 10.5 20.25 90)
    (property "Reference" "U1")
    (property "Value" "Regulator")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "VOUT_3V3"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "TestPoint:TestPoint_Pad"
    (layer "F.Cu")
    (property "Reference" "TP1")
    (property "Value" "TestPoint")
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (layers "*.Cu") (net 2 "VOUT_3V3"))
  )
)
""",
                encoding="utf-8",
            )
            result = import_board(source, "kicad-pcb", "kicad_pcb_demo", "KiCad PCB Demo", output)
            self.assertTrue(result["ok"])
            board = load_board_context(output)
            self.assertEqual(board.board_id, "kicad_pcb_demo")
            self.assertIn("VOUT_3V3", board.nets)
            self.assertEqual(board.components["U1"]["pins"][0]["net"], "VOUT_3V3")
            self.assertEqual(board.components["U1"]["placement"]["rotation_deg"], 90.0)
            self.assertEqual(board.test_points["TP1"]["net"], "VOUT_3V3")


    def test_import_kicad_sch_to_board_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "board.kicad_sch"
            output = Path(tmp) / "board.json"
            source.write_text(
                """
(kicad_sch
  (symbol
    (property "Reference" "U1")
    (property "Value" "Regulator")
    (property "Footprint" "Package_TO_SOT_SMD:SOT-23")
    (property "Part Number" "REG-123")
  )
  (symbol
    (property "Reference" "C1")
    (property "Value" "10uF")
    (property "Footprint" "Capacitor_SMD:C_0603")
  )
)
""",
                encoding="utf-8",
            )
            result = import_board(source, "kicad-sch", "kicad_sch_demo", "KiCad SCH Demo", output)
            self.assertTrue(result["ok"])
            board = load_board_context(output)
            self.assertEqual(board.board_id, "kicad_sch_demo")
            self.assertEqual(board.components["U1"]["package"], "Package_TO_SOT_SMD:SOT-23")
            self.assertEqual(board.components["U1"]["part_number"], "REG-123")
            self.assertEqual(board.components["C1"]["value"], "10uF")
            self.assertEqual(board.nets["UNASSIGNED"]["domain"], "unknown")


class JsonModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        payload = json.dumps(self.server.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def start_json_model_server(payload: dict[str, object]) -> tuple[HTTPServer, threading.Thread, str]:
    server = HTTPServer(("127.0.0.1", 0), JsonModelHandler)
    server.payload = payload
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}/model"


def stop_server(server: HTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

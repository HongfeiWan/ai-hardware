from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request

from ai_hardware_bench.bench import BenchApp
from ai_hardware_bench.data import load_board_context
from ai_hardware_bench.importers import import_board
from ai_hardware_bench.mcp_server import StdioJsonRpcServer
from ai_hardware_bench.regression import run_regression_suite
from ai_hardware_bench.report import generate_session_report
from ai_hardware_bench.web import create_console_server


ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "examples" / "boards" / "usb_power_stage.yaml"


class BenchPrototypeTest(unittest.TestCase):
    def test_load_yaml_board_without_external_dependencies(self) -> None:
        board = load_board_context(BOARD)
        self.assertEqual(board.board_id, "usb_power_stage_demo")
        self.assertIn("VOUT_3V3", board.nets)
        self.assertEqual(board.canonical_net("3V3"), "VOUT_3V3")

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

    def test_instrument_status_defaults_to_mock(self) -> None:
        app = BenchApp()
        status = app.instrument_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["instruments"][0]["backend"], "mock")
        self.assertEqual(status["instruments"][1]["backend"], "mock")

    def test_model_status_defaults_to_rules(self) -> None:
        app = BenchApp()
        status = app.model_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["model"]["backend"], "rules")

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
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["passed"], 2)

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


if __name__ == "__main__":
    unittest.main()

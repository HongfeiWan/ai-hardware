from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_hardware_bench.bench import BenchApp
from ai_hardware_bench.data import load_board_context
from ai_hardware_bench.mcp_server import StdioJsonRpcServer


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
            session = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(session["board_id"], "usb_power_stage_demo")
            self.assertGreaterEqual(len(session["measurements"]), 2)
            self.assertEqual(session["findings"][0]["severity"], "warning")
            artifact_path = Path(session["artifacts"][0]["uri"])
            self.assertTrue(artifact_path.exists())

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


if __name__ == "__main__":
    unittest.main()


#!/usr/bin/env python3
"""Non-destructive MCP smoke test for the ESP32 fixture firmware."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


CORE_TOOLS = {
    "fixture.ping",
    "fixture.get_status",
    "fixture.self_test",
    "fixture.set_mux_channel",
    "fixture.select_net",
    "fixture.set_runtime_net",
    "fixture.set_runtime_net_map",
    "fixture.clear_runtime_net",
    "fixture.clear_runtime_net_map",
    "fixture.reset_dut",
    "fixture.set_load_switch",
    "fixture.read_digital_input",
    "fixture.scan_digital_inputs",
}

OPTIONAL_TOOLS = {
    "fixture.read_adc_raw",
    "fixture.read_net_adc_raw",
    "fixture.scan_net_adc",
    "fixture.sample_net_adc_series",
}


class McpHttpClient:
    def __init__(self, url: str, protocol_version: str, timeout: float) -> None:
        self.url = url
        self.protocol_version = protocol_version
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 1

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        response = self._post(message)
        if response is None:
            raise RuntimeError(f"{method} returned no JSON response")
        if response.get("id") != request_id:
            raise RuntimeError(f"{method} returned mismatched id: {response!r}")
        if "error" in response:
            raise RuntimeError(f"{method} failed: {json.dumps(response['error'], ensure_ascii=False)}")
        return response["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            message["params"] = params
        self._post(message, allow_empty=True)

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": "ai-hardware-esp32-smoke",
                    "version": "0.1.0",
                },
            },
        )
        self.notify("notifications/initialized", {})
        return result

    def _post(self, message: dict, allow_empty: bool = False) -> dict | None:
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id

        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session_id = response.headers.get("MCP-Session-Id")
                if session_id:
                    self.session_id = session_id
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} from {self.url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot connect to {self.url}: {exc.reason}") from exc

        if not payload:
            if allow_empty:
                return None
            raise RuntimeError(f"Empty HTTP response from {self.url}")
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            text = payload.decode("utf-8", errors="replace")
            raise RuntimeError(f"Invalid JSON response: {text}") from exc


def tool_text_json(result: dict) -> dict:
    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise RuntimeError(f"Tool result has no content: {result!r}")
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        raise RuntimeError(f"Tool result is not text content: {result!r}")
    text = first.get("text")
    if not isinstance(text, str):
        raise RuntimeError(f"Tool text content is not a string: {result!r}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Tool text content is not JSON: {text}") from exc


def require_ok(name: str, payload: dict) -> None:
    if payload.get("ok") is not True:
        raise RuntimeError(f"{name} returned non-ok payload: {json.dumps(payload, ensure_ascii=False)}")


def require_adc_shape(name: str, payload: dict) -> None:
    for field in ("samples", "raw_avg", "raw_min", "raw_max", "raw_last"):
        if not isinstance(payload.get(field), int):
            raise RuntimeError(f"{name} missing integer field {field}: {json.dumps(payload, ensure_ascii=False)}")
    if payload.get("millivolts_valid") is True and not isinstance(payload.get("mv_avg"), int):
        raise RuntimeError(f"{name} reported millivolts_valid without mv_avg: {json.dumps(payload, ensure_ascii=False)}")
    if payload.get("scaled_millivolts_valid") is True and not isinstance(payload.get("scaled_mv_avg"), int):
        raise RuntimeError(
            f"{name} reported scaled_millivolts_valid without scaled_mv_avg: "
            f"{json.dumps(payload, ensure_ascii=False)}"
        )


def require_adc_series_shape(name: str, payload: dict) -> None:
    for field in ("points", "samples_per_point", "interval_ms"):
        if not isinstance(payload.get(field), int):
            raise RuntimeError(f"{name} missing integer field {field}: {json.dumps(payload, ensure_ascii=False)}")
    readings = payload.get("readings")
    if not isinstance(readings, list):
        raise RuntimeError(f"{name} missing readings list: {json.dumps(payload, ensure_ascii=False)}")
    if len(readings) != payload["points"]:
        raise RuntimeError(
            f"{name} readings length {len(readings)} does not match points {payload['points']}: "
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
    for index, reading in enumerate(readings):
        if not isinstance(reading, dict):
            raise RuntimeError(f"{name} reading {index} is not an object: {reading!r}")
        if reading.get("index") != index:
            raise RuntimeError(f"{name} reading index mismatch at {index}: {reading!r}")
        if not isinstance(reading.get("t_ms"), int):
            raise RuntimeError(f"{name} reading {index} missing integer t_ms: {reading!r}")
        require_adc_shape(f"{name}.readings[{index}]", reading)


def require_adc_scan_shape(name: str, payload: dict) -> None:
    for field in ("entry_count", "samples", "settle_ms", "scanned_count"):
        if not isinstance(payload.get(field), int):
            raise RuntimeError(f"{name} missing integer field {field}: {json.dumps(payload, ensure_ascii=False)}")
    readings = payload.get("readings")
    if not isinstance(readings, list):
        raise RuntimeError(f"{name} missing readings list: {json.dumps(payload, ensure_ascii=False)}")
    if len(readings) != payload["scanned_count"]:
        raise RuntimeError(
            f"{name} readings length {len(readings)} does not match scanned_count {payload['scanned_count']}: "
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
    if payload["scanned_count"] > payload["entry_count"]:
        raise RuntimeError(f"{name} scanned_count exceeds entry_count: {json.dumps(payload, ensure_ascii=False)}")
    for index, reading in enumerate(readings):
        if not isinstance(reading, dict):
            raise RuntimeError(f"{name} reading {index} is not an object: {reading!r}")
        if not isinstance(reading.get("net"), str) or not reading["net"]:
            raise RuntimeError(f"{name} reading {index} missing net label: {reading!r}")
        if not isinstance(reading.get("mux_channel"), int):
            raise RuntimeError(f"{name} reading {index} missing mux_channel: {reading!r}")
        if not isinstance(reading.get("t_ms"), int):
            raise RuntimeError(f"{name} reading {index} missing t_ms: {reading!r}")
        require_adc_shape(f"{name}.readings[{index}]", reading)


def require_digital_inputs_shape(name: str, payload: dict) -> None:
    if not isinstance(payload.get("entry_count"), int):
        raise RuntimeError(f"{name} missing entry_count: {json.dumps(payload, ensure_ascii=False)}")
    readings = payload.get("readings")
    if not isinstance(readings, list):
        raise RuntimeError(f"{name} missing readings list: {json.dumps(payload, ensure_ascii=False)}")
    if "scanned_count" in payload and payload["scanned_count"] != len(readings):
        raise RuntimeError(f"{name} scanned_count mismatch: {json.dumps(payload, ensure_ascii=False)}")
    for index, reading in enumerate(readings):
        if not isinstance(reading, dict):
            raise RuntimeError(f"{name} reading {index} is not an object: {reading!r}")
        for field in ("label",):
            if not isinstance(reading.get(field), str) or not reading[field]:
                raise RuntimeError(f"{name} reading {index} missing {field}: {reading!r}")
        for field in ("gpio", "raw_level"):
            if not isinstance(reading.get(field), int):
                raise RuntimeError(f"{name} reading {index} missing integer {field}: {reading!r}")
        for field in ("active", "active_low"):
            if not isinstance(reading.get(field), bool):
                raise RuntimeError(f"{name} reading {index} missing boolean {field}: {reading!r}")


def require_runtime_net_map_set_shape(name: str, payload: dict, expected_applied_count: int) -> None:
    if payload.get("applied_count") != expected_applied_count:
        raise RuntimeError(f"{name} applied_count mismatch: {json.dumps(payload, ensure_ascii=False)}")
    if not isinstance(payload.get("runtime_entry_count"), int):
        raise RuntimeError(f"{name} missing runtime_entry_count: {json.dumps(payload, ensure_ascii=False)}")
    if payload["runtime_entry_count"] < expected_applied_count:
        raise RuntimeError(f"{name} runtime_entry_count too small: {json.dumps(payload, ensure_ascii=False)}")
    if payload.get("persisted") is not True:
        raise RuntimeError(f"{name} did not report persisted=true: {json.dumps(payload, ensure_ascii=False)}")
    if not isinstance(payload.get("clear_existing"), bool):
        raise RuntimeError(f"{name} missing clear_existing boolean: {json.dumps(payload, ensure_ascii=False)}")


def find_net_map_entry(net_map: dict, net: str, source: str | None = None) -> dict | None:
    entries = net_map.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError(f"fixture://net-map entries are missing: {json.dumps(net_map, ensure_ascii=False)}")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("net") != net:
            continue
        if source is not None and entry.get("source") != source:
            continue
        return entry
    return None


def first_selectable_mux_channel(net_map: dict) -> int:
    entries = net_map.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError(f"fixture://net-map entries are missing: {json.dumps(net_map, ensure_ascii=False)}")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        channel = entry.get("mux_channel")
        if isinstance(channel, int) and entry.get("selectable") is not False:
            return channel
    mux_max_channel = net_map.get("mux_max_channel")
    if isinstance(mux_max_channel, int) and mux_max_channel >= 0:
        return 0
    raise RuntimeError(f"fixture://net-map has no selectable MUX channel: {json.dumps(net_map, ensure_ascii=False)}")


def resource_text_json(result: dict, expected_uri: str) -> dict:
    contents = result.get("contents")
    if not isinstance(contents, list) or not contents:
        raise RuntimeError(f"Resource result has no contents: {result!r}")
    first = contents[0]
    if not isinstance(first, dict):
        raise RuntimeError(f"Resource content is not an object: {result!r}")
    if first.get("uri") != expected_uri:
        raise RuntimeError(f"Resource uri mismatch: {first.get('uri')!r} != {expected_uri!r}")
    if first.get("mimeType") != "application/json":
        raise RuntimeError(f"Resource mimeType mismatch: {first.get('mimeType')!r}")
    text = first.get("text")
    if not isinstance(text, str):
        raise RuntimeError(f"Resource text is not a string: {result!r}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Resource text is not JSON: {text}") from exc


def initialize_client(url: str, protocol_version: str, timeout: float, wait_ready: float) -> tuple[McpHttpClient, dict]:
    if wait_ready < 0:
        raise RuntimeError("--wait-ready must be zero or positive")

    deadline = time.monotonic() + wait_ready if wait_ready > 0 else None
    reported_wait = False

    while True:
        client = McpHttpClient(url, protocol_version, timeout)
        try:
            init_result = client.initialize()
            return client, init_result
        except RuntimeError as exc:
            if deadline is None or time.monotonic() >= deadline:
                raise
            if not reported_wait:
                print(f"Waiting up to {wait_ready:g}s for MCP endpoint {url}...", file=sys.stderr)
                reported_wait = True
            sleep_for = min(1.0, max(0.1, deadline - time.monotonic()))
            time.sleep(sleep_for)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test the ESP32 fixture MCP endpoint.")
    parser.add_argument("--host", default="192.168.4.1", help="ESP32 host or IP address")
    parser.add_argument("--port", type=int, default=80, help="MCP HTTP port")
    parser.add_argument("--endpoint", default="mcp", help="MCP endpoint path without leading slash")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--wait-ready",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Retry MCP initialize until the endpoint is ready or this timeout expires",
    )
    parser.add_argument(
        "--protocol-version",
        default="2025-03-26",
        help="MCP protocol version to negotiate with the firmware",
    )
    parser.add_argument(
        "--skip-adc-tool",
        action="store_true",
        help="Do not require fixture.read_adc_raw, useful after disabling ADC in menuconfig",
    )
    parser.add_argument(
        "--exercise-net-adc-tool",
        action="store_true",
        help="Call net ADC tools on the first configured net; this changes the MUX selection",
    )
    parser.add_argument(
        "--exercise-runtime-net",
        action="store_true",
        help="Temporarily persist, select and clear a runtime net mapping; this changes NVS and MUX selection",
    )
    parser.add_argument(
        "--runtime-net-label",
        default="__SMOKE_RUNTIME_NET__",
        help="Temporary runtime net label used with --exercise-runtime-net",
    )
    parser.add_argument(
        "--runtime-net-channel",
        type=int,
        help="MUX channel used with --exercise-runtime-net; defaults to the first selectable net-map channel",
    )
    args = parser.parse_args()

    endpoint = args.endpoint.strip("/")
    url = f"http://{args.host}:{args.port}/{endpoint}"

    try:
        client, init_result = initialize_client(url, args.protocol_version, args.timeout, args.wait_ready)
        standard_ping = client.request("ping", {})
        tools_result = client.request("tools/list", {})
        tool_names = {tool.get("name") for tool in tools_result.get("tools", []) if isinstance(tool, dict)}
        expected_tools = set(CORE_TOOLS)
        if not args.skip_adc_tool:
            expected_tools |= OPTIONAL_TOOLS
        missing = sorted(expected_tools - tool_names)
        if missing:
            raise RuntimeError(f"Missing expected tools: {', '.join(missing)}")

        fixture_ping = tool_text_json(
            client.request("tools/call", {"name": "fixture.ping", "arguments": {}})
        )
        require_ok("fixture.ping", fixture_ping)
        fixture_status = tool_text_json(
            client.request("tools/call", {"name": "fixture.get_status", "arguments": {}})
        )
        require_ok("fixture.get_status", fixture_status)
        fixture_self_test = tool_text_json(
            client.request("tools/call", {"name": "fixture.self_test", "arguments": {}})
        )
        require_ok("fixture.self_test", fixture_self_test)
        resource_result = client.request("resources/read", {"uri": "fixture://status"})
        resource_status = resource_text_json(resource_result, "fixture://status")
        require_ok("fixture://status", resource_status)
        if fixture_status.get("device") != resource_status.get("device"):
            raise RuntimeError("fixture.get_status and fixture://status report different devices")
        net_map_result = client.request("resources/read", {"uri": "fixture://net-map"})
        net_map = resource_text_json(net_map_result, "fixture://net-map")
        require_ok("fixture://net-map", net_map)
        if net_map.get("entry_count", 0) < 1:
            raise RuntimeError("fixture://net-map has no enabled net entries")
        digital_inputs_result = client.request("resources/read", {"uri": "fixture://digital-inputs"})
        digital_inputs = resource_text_json(digital_inputs_result, "fixture://digital-inputs")
        require_ok("fixture://digital-inputs", digital_inputs)
        digital_scan_result = tool_text_json(
            client.request("tools/call", {"name": "fixture.scan_digital_inputs", "arguments": {}})
        )
        require_ok("fixture.scan_digital_inputs", digital_scan_result)
        require_digital_inputs_shape("fixture.scan_digital_inputs", digital_scan_result)
        digital_read_result = None
        digital_entries = digital_inputs.get("entries")
        if isinstance(digital_entries, list) and digital_entries:
            first_digital_label = digital_entries[0].get("label")
            if not isinstance(first_digital_label, str) or not first_digital_label:
                raise RuntimeError(f"First digital input entry has no usable label: {digital_entries[0]!r}")
            digital_read_result = tool_text_json(
                client.request(
                    "tools/call",
                    {"name": "fixture.read_digital_input", "arguments": {"label": first_digital_label}},
                )
            )
            require_ok("fixture.read_digital_input", digital_read_result)
            require_digital_inputs_shape(
                "fixture.read_digital_input",
                {"entry_count": 1, "readings": [digital_read_result], "scanned_count": 1},
            )
        adc_result = None
        net_adc_result = None
        net_adc_scan_result = None
        net_adc_series_result = None
        runtime_set_result = None
        runtime_select_result = None
        runtime_clear_result = None
        runtime_net_map_after_set = None
        runtime_net_map_after_clear = None
        if not args.skip_adc_tool:
            adc_result = tool_text_json(
                client.request("tools/call", {"name": "fixture.read_adc_raw", "arguments": {"samples": 1}})
            )
            require_ok("fixture.read_adc_raw", adc_result)
            require_adc_shape("fixture.read_adc_raw", adc_result)
            if args.exercise_net_adc_tool:
                entries = net_map.get("entries")
                if not isinstance(entries, list) or not entries:
                    raise RuntimeError("fixture://net-map entries are missing")
                first_net = entries[0].get("net")
                if not isinstance(first_net, str) or not first_net:
                    raise RuntimeError(f"First net-map entry has no usable net label: {entries[0]!r}")
                net_adc_result = tool_text_json(
                    client.request(
                        "tools/call",
                        {
                            "name": "fixture.read_net_adc_raw",
                            "arguments": {"net": first_net, "samples": 1, "settle_ms": 1},
                        },
                    )
                )
                require_ok("fixture.read_net_adc_raw", net_adc_result)
                require_adc_shape("fixture.read_net_adc_raw", net_adc_result)
                net_adc_scan_result = tool_text_json(
                    client.request(
                        "tools/call",
                        {
                            "name": "fixture.scan_net_adc",
                            "arguments": {"samples": 1, "settle_ms": 1},
                        },
                    )
                )
                require_ok("fixture.scan_net_adc", net_adc_scan_result)
                require_adc_scan_shape("fixture.scan_net_adc", net_adc_scan_result)
                net_adc_series_result = tool_text_json(
                    client.request(
                        "tools/call",
                        {
                            "name": "fixture.sample_net_adc_series",
                            "arguments": {
                                "net": first_net,
                                "points": 2,
                                "samples_per_point": 1,
                                "interval_ms": 1,
                                "settle_ms": 1,
                            },
                        },
                    )
                )
                require_ok("fixture.sample_net_adc_series", net_adc_series_result)
                require_adc_series_shape("fixture.sample_net_adc_series", net_adc_series_result)
        elif args.exercise_net_adc_tool:
            raise RuntimeError("--exercise-net-adc-tool cannot be used with --skip-adc-tool")

        if args.exercise_runtime_net:
            runtime_label = args.runtime_net_label
            if not isinstance(runtime_label, str) or not runtime_label:
                raise RuntimeError("--runtime-net-label must be a non-empty string")
            if find_net_map_entry(net_map, runtime_label) is not None:
                raise RuntimeError(
                    f"Temporary runtime net label {runtime_label!r} already exists; "
                    "choose a different --runtime-net-label"
                )
            runtime_channel = (
                args.runtime_net_channel
                if args.runtime_net_channel is not None
                else first_selectable_mux_channel(net_map)
            )

            cleanup_needed = False
            runtime_error: RuntimeError | None = None
            try:
                runtime_set_result = tool_text_json(
                    client.request(
                        "tools/call",
                        {
                            "name": "fixture.set_runtime_net_map",
                            "arguments": {
                                "mappings": [{"net": runtime_label, "channel": runtime_channel}],
                                "clear_existing": False,
                            },
                        },
                    )
                )
                require_ok("fixture.set_runtime_net_map", runtime_set_result)
                require_runtime_net_map_set_shape("fixture.set_runtime_net_map", runtime_set_result, 1)
                cleanup_needed = True

                runtime_net_map_after_set = resource_text_json(
                    client.request("resources/read", {"uri": "fixture://net-map"}),
                    "fixture://net-map",
                )
                require_ok("fixture://net-map after fixture.set_runtime_net_map", runtime_net_map_after_set)
                runtime_entry = find_net_map_entry(runtime_net_map_after_set, runtime_label, source="runtime")
                if runtime_entry is None:
                    raise RuntimeError("fixture.set_runtime_net_map did not appear in fixture://net-map")
                if runtime_entry.get("mux_channel") != runtime_channel:
                    raise RuntimeError(f"Runtime net channel mismatch: {runtime_entry!r}")

                runtime_select_result = tool_text_json(
                    client.request(
                        "tools/call",
                        {"name": "fixture.select_net", "arguments": {"net": runtime_label}},
                    )
                )
                require_ok("fixture.select_net runtime", runtime_select_result)
                if runtime_select_result.get("source") != "runtime":
                    raise RuntimeError(f"fixture.select_net did not use runtime source: {runtime_select_result!r}")
                if runtime_select_result.get("mux_channel") != runtime_channel:
                    raise RuntimeError(f"fixture.select_net channel mismatch: {runtime_select_result!r}")
            except RuntimeError as exc:
                runtime_error = exc

            if cleanup_needed:
                try:
                    runtime_clear_result = tool_text_json(
                        client.request(
                            "tools/call",
                            {"name": "fixture.clear_runtime_net", "arguments": {"net": runtime_label}},
                        )
                    )
                    require_ok("fixture.clear_runtime_net", runtime_clear_result)
                    runtime_net_map_after_clear = resource_text_json(
                        client.request("resources/read", {"uri": "fixture://net-map"}),
                        "fixture://net-map",
                    )
                    require_ok("fixture://net-map after fixture.clear_runtime_net", runtime_net_map_after_clear)
                    if find_net_map_entry(runtime_net_map_after_clear, runtime_label) is not None:
                        raise RuntimeError("fixture.clear_runtime_net left the temporary net in fixture://net-map")
                except RuntimeError as cleanup_exc:
                    if runtime_error is not None:
                        raise RuntimeError(
                            f"{runtime_error}; runtime net cleanup also failed: {cleanup_exc}"
                        ) from cleanup_exc
                    raise

            if runtime_error is not None:
                raise runtime_error

    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    server_info = init_result.get("serverInfo", {})
    print("OK: ESP32 fixture MCP endpoint responded")
    print(f"URL: {url}")
    print(f"Session: {client.session_id}")
    print(f"Server: {server_info.get('title') or server_info.get('name')}")
    print(f"Standard ping: {standard_ping}")
    print(f"Tools: {', '.join(sorted(tool_names))}")
    print(f"Fixture ping: {json.dumps(fixture_ping, ensure_ascii=False)}")
    print(f"Fixture status: {json.dumps(fixture_status, ensure_ascii=False)}")
    print(f"Fixture self test: {json.dumps(fixture_self_test, ensure_ascii=False)}")
    print(f"Status resource: {json.dumps(resource_status, ensure_ascii=False)}")
    print(f"Net map resource: {json.dumps(net_map, ensure_ascii=False)}")
    print(f"Digital inputs resource: {json.dumps(digital_inputs, ensure_ascii=False)}")
    print(f"Digital input scan: {json.dumps(digital_scan_result, ensure_ascii=False)}")
    if digital_read_result is not None:
        print(f"Digital input read: {json.dumps(digital_read_result, ensure_ascii=False)}")
    if adc_result is not None:
        print(f"ADC read: {json.dumps(adc_result, ensure_ascii=False)}")
    if net_adc_result is not None:
        print(f"Net ADC read: {json.dumps(net_adc_result, ensure_ascii=False)}")
    if net_adc_scan_result is not None:
        print(f"Net ADC scan: {json.dumps(net_adc_scan_result, ensure_ascii=False)}")
    if net_adc_series_result is not None:
        print(f"Net ADC series: {json.dumps(net_adc_series_result, ensure_ascii=False)}")
    if runtime_set_result is not None:
        print(f"Runtime net set: {json.dumps(runtime_set_result, ensure_ascii=False)}")
    if runtime_net_map_after_set is not None:
        print(f"Runtime net map after set: {json.dumps(runtime_net_map_after_set, ensure_ascii=False)}")
    if runtime_select_result is not None:
        print(f"Runtime net select: {json.dumps(runtime_select_result, ensure_ascii=False)}")
    if runtime_clear_result is not None:
        print(f"Runtime net clear: {json.dumps(runtime_clear_result, ensure_ascii=False)}")
    if runtime_net_map_after_clear is not None:
        print(f"Runtime net map after clear: {json.dumps(runtime_net_map_after_clear, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build, flash and monitor the ESP32 fixture firmware."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import glob
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
BUILD_DIR = PROJECT_DIR / "build"
DEFAULT_BUNDLE_DIR = PROJECT_DIR / "dist" / "esp32-fixture"
SDKCONFIG = PROJECT_DIR / "sdkconfig"
DEFAULT_BAUD = 460800
BUILD_BIN = BUILD_DIR / "ai_hardware_esp32_fixture.bin"
BOOTLOADER_BIN = BUILD_DIR / "bootloader" / "bootloader.bin"
PARTITION_BIN = BUILD_DIR / "partition_table" / "partition-table.bin"
FLASH_ARGS = BUILD_DIR / "flash_args"
FLASHER_ARGS_JSON = BUILD_DIR / "flasher_args.json"
PROJECT_DESCRIPTION_JSON = BUILD_DIR / "project_description.json"
SMOKE_TEST = PROJECT_DIR / "tools" / "smoke_test.py"
PARTITIONS_CSV = PROJECT_DIR / "partitions.csv"
DEFAULT_MCP_HOST = "192.168.4.1"
DEFAULT_MCP_PORT = 80
DEFAULT_MCP_ENDPOINT = "mcp"
DEFAULT_MCP_PROTOCOL_VERSION = "2025-03-26"
RUNTIME_NET_MAP_SLOT_COUNT = 8
RUNTIME_NET_LABEL_MAX_LEN = 31
PORT_PATTERNS = (
    "/dev/cu.usbserial*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/cu.usbmodem*",
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
    "/dev/tty.usbserial*",
    "/dev/tty.SLAB_USBtoUART*",
    "/dev/tty.usbmodem*",
)

MANIFEST_CONFIG_KEYS = (
    "CONFIG_FIXTURE_WIFI_MODE_AP",
    "CONFIG_FIXTURE_WIFI_MODE_STA",
    "CONFIG_FIXTURE_WIFI_AP_SSID_PREFIX",
    "CONFIG_FIXTURE_WIFI_AP_PASSWORD",
    "CONFIG_FIXTURE_WIFI_AP_CHANNEL",
    "CONFIG_FIXTURE_WIFI_AP_MAX_CONN",
    "CONFIG_FIXTURE_WIFI_STA_SSID",
    "CONFIG_FIXTURE_MCP_PORT",
    "CONFIG_FIXTURE_MCP_ENDPOINT",
    "CONFIG_FIXTURE_RESET_GPIO",
    "CONFIG_FIXTURE_LOAD_SWITCH_GPIO",
    "CONFIG_FIXTURE_MUX_SEL0_GPIO",
    "CONFIG_FIXTURE_MUX_SEL1_GPIO",
    "CONFIG_FIXTURE_MUX_SEL2_GPIO",
    "CONFIG_FIXTURE_MUX_MAX_CHANNEL",
    "CONFIG_FIXTURE_MUX_SETTLE_MS",
    "CONFIG_FIXTURE_DIN0_LABEL",
    "CONFIG_FIXTURE_DIN0_GPIO",
    "CONFIG_FIXTURE_DIN0_ACTIVE_LOW",
    "CONFIG_FIXTURE_DIN0_PULLUP",
    "CONFIG_FIXTURE_DIN0_PULLDOWN",
    "CONFIG_FIXTURE_DIN1_LABEL",
    "CONFIG_FIXTURE_DIN1_GPIO",
    "CONFIG_FIXTURE_DIN1_ACTIVE_LOW",
    "CONFIG_FIXTURE_DIN1_PULLUP",
    "CONFIG_FIXTURE_DIN1_PULLDOWN",
    "CONFIG_FIXTURE_DIN2_LABEL",
    "CONFIG_FIXTURE_DIN2_GPIO",
    "CONFIG_FIXTURE_DIN2_ACTIVE_LOW",
    "CONFIG_FIXTURE_DIN2_PULLUP",
    "CONFIG_FIXTURE_DIN2_PULLDOWN",
    "CONFIG_FIXTURE_DIN3_LABEL",
    "CONFIG_FIXTURE_DIN3_GPIO",
    "CONFIG_FIXTURE_DIN3_ACTIVE_LOW",
    "CONFIG_FIXTURE_DIN3_PULLUP",
    "CONFIG_FIXTURE_DIN3_PULLDOWN",
    "CONFIG_FIXTURE_ADC_ENABLE",
    "CONFIG_FIXTURE_ADC_UNIT",
    "CONFIG_FIXTURE_ADC_CHANNEL",
    "CONFIG_FIXTURE_ADC_CALIBRATION_ENABLE",
    "CONFIG_FIXTURE_ADC_DEFAULT_VREF_MV",
    "CONFIG_FIXTURE_ADC_SCALE_NUMERATOR",
    "CONFIG_FIXTURE_ADC_SCALE_DENOMINATOR",
    "CONFIG_FIXTURE_ADC_OFFSET_MV",
    "CONFIG_FIXTURE_ADC_SERIES_MAX_POINTS",
    "CONFIG_FIXTURE_ADC_SERIES_MAX_SAMPLES_PER_POINT",
    "CONFIG_FIXTURE_ADC_SERIES_MAX_INTERVAL_MS",
    "CONFIG_FIXTURE_ADC_SERIES_MAX_TOTAL_MS",
    "CONFIG_FIXTURE_NET0_LABEL",
    "CONFIG_FIXTURE_NET0_CHANNEL",
    "CONFIG_FIXTURE_NET1_LABEL",
    "CONFIG_FIXTURE_NET1_CHANNEL",
    "CONFIG_FIXTURE_NET2_LABEL",
    "CONFIG_FIXTURE_NET2_CHANNEL",
    "CONFIG_FIXTURE_NET3_LABEL",
    "CONFIG_FIXTURE_NET3_CHANNEL",
    "CONFIG_FIXTURE_NET4_LABEL",
    "CONFIG_FIXTURE_NET4_CHANNEL",
    "CONFIG_FIXTURE_NET5_LABEL",
    "CONFIG_FIXTURE_NET5_CHANNEL",
    "CONFIG_FIXTURE_NET6_LABEL",
    "CONFIG_FIXTURE_NET6_CHANNEL",
    "CONFIG_FIXTURE_NET7_LABEL",
    "CONFIG_FIXTURE_NET7_CHANNEL",
)


def run_idf(args: list[str]) -> None:
    if shutil.which("idf.py") is None:
        raise RuntimeError("idf.py not found. Source ESP-IDF export.sh first, then retry.")
    subprocess.run(["idf.py", *args], cwd=PROJECT_DIR, check=True)


def run_idf_capture(args: list[str]) -> str:
    if shutil.which("idf.py") is None:
        return "not available"
    result = subprocess.run(["idf.py", *args], cwd=PROJECT_DIR, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def run_smoke_test(args: argparse.Namespace) -> None:
    command = [
        sys.executable,
        str(SMOKE_TEST),
        "--host",
        args.host,
        "--port",
        str(args.http_port),
        "--endpoint",
        args.endpoint,
        "--timeout",
        str(args.timeout),
        "--wait-ready",
        str(args.wait_ready),
        "--protocol-version",
        args.protocol_version,
    ]
    if args.skip_adc_tool:
        command.append("--skip-adc-tool")
    if args.exercise_net_adc_tool:
        command.append("--exercise-net-adc-tool")
    if args.exercise_runtime_net:
        command.append("--exercise-runtime-net")
        command.extend(["--runtime-net-label", args.runtime_net_label])
        if args.runtime_net_channel is not None:
            command.extend(["--runtime-net-channel", str(args.runtime_net_channel)])
    subprocess.run(command, cwd=PROJECT_DIR, check=True)


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
                    "name": "ai-hardware-esp32-deploy",
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


def endpoint_url(args: argparse.Namespace) -> str:
    endpoint = args.endpoint.strip("/")
    return f"http://{args.host}:{args.http_port}/{endpoint}"


def initialize_mcp_client(args: argparse.Namespace) -> tuple[McpHttpClient, dict]:
    if args.wait_ready < 0:
        raise RuntimeError("--wait-ready must be zero or positive")

    url = endpoint_url(args)
    deadline = time.monotonic() + args.wait_ready if args.wait_ready > 0 else None
    reported_wait = False
    while True:
        client = McpHttpClient(url, args.protocol_version, args.timeout)
        try:
            init_result = client.initialize()
            return client, init_result
        except RuntimeError:
            if deadline is None or time.monotonic() >= deadline:
                raise
            if not reported_wait:
                print(f"Waiting up to {args.wait_ready:g}s for MCP endpoint {url}...")
                reported_wait = True
            time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))


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


def resource_text_json(result: dict, expected_uri: str) -> dict:
    contents = result.get("contents")
    if not isinstance(contents, list) or not contents:
        raise RuntimeError(f"Resource result has no contents: {result!r}")
    first = contents[0]
    if not isinstance(first, dict) or first.get("uri") != expected_uri:
        raise RuntimeError(f"Resource uri mismatch for {expected_uri}: {result!r}")
    text = first.get("text")
    if not isinstance(text, str):
        raise RuntimeError(f"Resource text is not a string: {result!r}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Resource text is not JSON: {text}") from exc


def target_is_configured() -> bool:
    if not SDKCONFIG.exists():
        return False
    return 'CONFIG_IDF_TARGET="esp32"' in SDKCONFIG.read_text(encoding="utf-8", errors="ignore")


def ensure_target() -> None:
    if not target_is_configured():
        run_idf(["set-target", "esp32"])


def prepare_build_config() -> None:
    ensure_target()
    run_idf(["reconfigure"])


def ensure_build_outputs() -> None:
    missing = [
        str(path.relative_to(PROJECT_DIR))
        for path in (BUILD_BIN, BOOTLOADER_BIN, PARTITION_BIN, FLASH_ARGS, FLASHER_ARGS_JSON)
        if not path.exists()
    ]
    if missing:
        raise RuntimeError("Missing build outputs. Run `python3 tools/deploy.py build` first:\n  " + "\n  ".join(missing))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Not an integer: {value!r}")
    return int(value.strip(), 0)


def safe_bundle_path(bundle_dir: Path, relative_name: object) -> Path:
    if not isinstance(relative_name, str) or not relative_name:
        raise RuntimeError(f"Invalid bundle file path in manifest: {relative_name!r}")
    relative_path = Path(relative_name)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RuntimeError(f"Unsafe bundle file path in manifest: {relative_name!r}")
    return bundle_dir / relative_path


def validate_zip_member_name(name: str) -> None:
    if not name or "\\" in name:
        raise RuntimeError(f"Unsafe zip member path: {name!r}")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe zip member path: {name!r}")


def extracted_bundle_root(extract_dir: Path) -> Path:
    if (extract_dir / "manifest.json").exists():
        return extract_dir

    candidates = [
        path
        for path in extract_dir.iterdir()
        if path.is_dir() and (path / "manifest.json").exists()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError("Zip bundle does not contain manifest.json at the root or in one top-level directory")
    raise RuntimeError("Zip bundle contains multiple top-level manifest.json files")


@contextmanager
def open_bundle_source(bundle: Path):
    source = bundle.resolve()
    if source.is_dir():
        yield source
        return
    if not source.exists():
        raise RuntimeError(f"Bundle does not exist: {source}")
    if not source.is_file() or source.suffix.lower() != ".zip":
        raise RuntimeError(f"Bundle must be a directory or .zip archive: {source}")

    try:
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                validate_zip_member_name(member.filename)
            with tempfile.TemporaryDirectory(prefix="aihw-esp32-bundle-") as temp_dir:
                extract_dir = Path(temp_dir)
                archive.extractall(extract_dir)
                yield extracted_bundle_root(extract_dir)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Invalid zip bundle: {source}") from exc


def factory_app_partition() -> tuple[int, int]:
    if not PARTITIONS_CSV.exists():
        raise RuntimeError(f"Missing partition table: {PARTITIONS_CSV}")

    with PARTITIONS_CSV.open(newline="", encoding="utf-8") as handle:
        rows = csv.reader(line for line in handle if not line.lstrip().startswith("#"))
        for row in rows:
            if len(row) < 5:
                continue
            name, partition_type, subtype, offset, size = [cell.strip() for cell in row[:5]]
            if name == "factory" and partition_type == "app" and subtype == "factory":
                return parse_int(offset), parse_int(size)
    raise RuntimeError(f"No factory app partition found in {PARTITIONS_CSV}")


def parse_flash_args(path: Path) -> dict[str, str]:
    if not path.exists():
        raise RuntimeError(f"Missing flash args: {path}")

    flash_files: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise RuntimeError(f"Unexpected flash_args line: {raw_line!r}")
        offset, relative_name = parts
        parse_int(offset)
        flash_files[hex(parse_int(offset))] = relative_name
    return flash_files


def bundle_file_entry(bundle_dir: Path, relative_name: str) -> dict[str, object]:
    path = safe_bundle_path(bundle_dir, relative_name)
    if not path.exists():
        raise RuntimeError(f"Bundled file is missing: {path}")
    return {
        "path": relative_name,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def read_sdkconfig_values(keys: tuple[str, ...]) -> dict[str, str]:
    if not SDKCONFIG.exists():
        return {}
    values: dict[str, str] = {}
    wanted = set(keys)
    for line in SDKCONFIG.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in wanted:
            values[key] = value.strip().strip('"')
    return values


def read_project_description() -> dict:
    if not PROJECT_DESCRIPTION_JSON.exists():
        return {}
    try:
        return json.loads(PROJECT_DESCRIPTION_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def idf_version_for_manifest(project_description: dict) -> str:
    version = run_idf_capture(["--version"])
    if version != "not available":
        return version
    git_revision = project_description.get("git_revision")
    if isinstance(git_revision, str) and git_revision:
        return git_revision
    return "not available"


def esptool_version() -> str | None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "esptool", "version"],
            check=True,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else "available"


def ensure_esptool_available() -> None:
    if esptool_version() is not None:
        return
    raise RuntimeError(
        f"esptool is not available for {sys.executable}. "
        "Source the ESP-IDF export.sh environment, or install esptool into this Python, then retry."
    )


def flash_command(baud: int) -> str:
    return (
        f"python -m esptool --chip esp32 -b {baud} --before default_reset --after hard_reset "
        "write_flash @flash_args"
    )


def erase_flash_command(baud: int) -> str:
    return (
        f"python -m esptool --chip esp32 -b {baud} --before default_reset --after hard_reset "
        "erase_flash"
    )


def prefer_callout_ports(ports: set[str]) -> list[str]:
    preferred: set[str] = set()
    for port in ports:
        if port.startswith("/dev/tty."):
            callout = "/dev/cu." + port.removeprefix("/dev/tty.")
            if callout in ports:
                continue
        preferred.add(port)
    return sorted(preferred)


def discover_ports() -> list[str]:
    ports: set[str] = set()
    for pattern in PORT_PATTERNS:
        ports.update(glob.glob(pattern))
    return prefer_callout_ports(ports)


def resolve_port(port: str | None, wait_port: float = 0.0) -> str:
    if wait_port < 0:
        raise RuntimeError("--wait-port must be zero or positive")
    deadline = time.monotonic() + wait_port if wait_port > 0 else None

    if port:
        if deadline is not None:
            print(f"Waiting up to {wait_port:g}s for serial port {port}...")
            while not Path(port).exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Serial port did not appear before timeout: {port}")
                time.sleep(0.5)
        return port

    if deadline is not None:
        print(f"Waiting up to {wait_port:g}s for one ESP32 serial port...")

    while True:
        ports = discover_ports()
        if len(ports) == 1:
            return ports[0]
        if len(ports) > 1:
            raise RuntimeError("Multiple serial ports found. Pass --port explicitly:\n  " + "\n  ".join(ports))
        if deadline is None or time.monotonic() >= deadline:
            raise RuntimeError("No serial ports found. Pass --port /dev/ttyUSB0 or the macOS /dev/cu.* port.")
        time.sleep(0.5)


def build(_: argparse.Namespace) -> None:
    prepare_build_config()
    run_idf(["build"])


def flash(args: argparse.Namespace) -> None:
    ensure_target()
    if not args.no_build:
        prepare_build_config()
        run_idf(["build"])
    port = resolve_port(args.port, args.wait_port)
    if args.erase_flash:
        run_idf(["-p", port, "-b", str(args.baud), "erase-flash"])
    run_idf(["-p", port, "-b", str(args.baud), "flash"])


def monitor(args: argparse.Namespace) -> None:
    port = resolve_port(args.port, args.wait_port)
    run_idf(["-p", port, "monitor"])


def flash_monitor(args: argparse.Namespace) -> None:
    ensure_target()
    if not args.no_build:
        prepare_build_config()
        run_idf(["build"])
    port = resolve_port(args.port, args.wait_port)
    if args.erase_flash:
        run_idf(["-p", port, "-b", str(args.baud), "erase-flash"])
    run_idf(["-p", port, "-b", str(args.baud), "flash", "monitor"])


def smoke(args: argparse.Namespace) -> None:
    run_smoke_test(args)


def parse_json_bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise RuntimeError(f"{field_name} must be a JSON boolean")


def validate_runtime_net_mappings(mappings: list[dict[str, object]], max_channel: int | None = None) -> None:
    if len(mappings) > RUNTIME_NET_MAP_SLOT_COUNT:
        raise RuntimeError(f"Too many mappings: {len(mappings)} > {RUNTIME_NET_MAP_SLOT_COUNT}")

    seen: set[str] = set()
    for index, item in enumerate(mappings):
        net = item["net"]
        channel = item["channel"]
        if not isinstance(net, str) or not net:
            raise RuntimeError(f"Mapping {index} has no non-empty string net")
        if len(net) > RUNTIME_NET_LABEL_MAX_LEN:
            raise RuntimeError(f"Mapping {index} net label is too long: {len(net)} > {RUNTIME_NET_LABEL_MAX_LEN}")
        if net in seen:
            raise RuntimeError(f"Duplicate net label in mappings: {net}")
        seen.add(net)
        if not isinstance(channel, int) or isinstance(channel, bool):
            raise RuntimeError(f"Mapping {index} has no integer channel")
        if channel < 0:
            raise RuntimeError(f"Mapping {index} channel must be non-negative")
        if max_channel is not None and channel > max_channel:
            raise RuntimeError(f"Mapping {index} channel {channel} exceeds max channel {max_channel}")


def configured_mux_max_channel() -> int | None:
    values = read_sdkconfig_values(("CONFIG_FIXTURE_MUX_MAX_CHANNEL",))
    value = values.get("CONFIG_FIXTURE_MUX_MAX_CHANNEL")
    if value is None:
        return None
    try:
        return parse_int(value)
    except ValueError:
        return None


def read_runtime_net_mappings(args: argparse.Namespace) -> tuple[list[dict[str, object]], bool]:
    if bool(args.mappings_json) == bool(args.mappings_file):
        raise RuntimeError("Pass exactly one of --mappings-json or --mappings-file")

    source = args.mappings_json
    if args.mappings_file:
        source = args.mappings_file.read_text(encoding="utf-8")

    try:
        data = json.loads(source)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid mappings JSON: {exc}") from exc

    clear_existing = args.clear_existing
    if isinstance(data, dict):
        if "clear_existing" in data:
            clear_existing = clear_existing or parse_json_bool(data["clear_existing"], "clear_existing")
        data = data.get("mappings")

    if not isinstance(data, list):
        raise RuntimeError("Mappings JSON must be an array or an object with a mappings array")

    mappings: list[dict[str, object]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise RuntimeError(f"Mapping {index} is not an object")
        net = item.get("net")
        channel = item.get("channel")
        mappings.append({"net": net, "channel": channel})
    validate_runtime_net_mappings(mappings, args.max_channel)
    return mappings, clear_existing


def load_net_map(args: argparse.Namespace) -> None:
    mappings, clear_existing = read_runtime_net_mappings(args)
    if args.dry_run:
        print("OK: runtime net map JSON is valid")
        print(
            json.dumps(
                {
                    "clear_existing": clear_existing,
                    "mapping_count": len(mappings),
                    "mappings": mappings,
                },
                ensure_ascii=False,
            )
        )
        return

    client, init_result = initialize_mcp_client(args)
    payload = tool_text_json(
        client.request(
            "tools/call",
            {
                "name": "fixture.set_runtime_net_map",
                "arguments": {
                    "mappings": mappings,
                    "clear_existing": clear_existing,
                },
            },
        )
    )
    if payload.get("ok") is not True:
        raise RuntimeError(f"fixture.set_runtime_net_map failed: {json.dumps(payload, ensure_ascii=False)}")

    print("OK: runtime net map loaded")
    print(f"URL: {endpoint_url(args)}")
    print(f"Session: {client.session_id}")
    print(f"Server: {init_result.get('serverInfo', {}).get('title') or init_result.get('serverInfo', {}).get('name')}")
    print(f"Result: {json.dumps(payload, ensure_ascii=False)}")

    if args.read_back:
        net_map = resource_text_json(client.request("resources/read", {"uri": "fixture://net-map"}), "fixture://net-map")
        print(f"Net map: {json.dumps(net_map, ensure_ascii=False)}")


def erase_flash(args: argparse.Namespace) -> None:
    ensure_target()
    port = resolve_port(args.port, args.wait_port)
    run_idf(["-p", port, "-b", str(args.baud), "erase-flash"])


def run_esptool(port: str, baud: int, esptool_args: list[str], cwd: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        "esp32",
        "-p",
        port,
        "-b",
        str(baud),
        "--before",
        "default_reset",
        "--after",
        "hard_reset",
        *esptool_args,
    ]
    subprocess.run(command, cwd=cwd, check=True)


def flash_bundle(args: argparse.Namespace) -> None:
    source = args.bundle.resolve()
    with open_bundle_source(args.bundle) as bundle_dir:
        summary = validate_bundle_dir(bundle_dir)
        ensure_esptool_available()
        port = resolve_port(args.port, args.wait_port)
        if args.erase_flash:
            run_esptool(port, args.baud, ["erase_flash"], bundle_dir)
        run_esptool(port, args.baud, ["write_flash", "@flash_args"], bundle_dir)

    print(f"Flashed verified bundle from {source}.")
    print(f"App: {summary['app_path']} ({summary['app_size']} bytes at {summary['app_offset']})")
    print(f"MCP endpoint: http://{args.host}:{args.http_port}/{args.endpoint}")

    if not args.smoke:
        print("Run `python3 tools/deploy.py smoke --wait-ready 30` after connecting to the fixture network.")
        return

    if args.prompt and sys.stdin.isatty():
        input("Connect this computer to the ESP32 fixture Wi-Fi, then press Enter to run the smoke test...")
    elif args.post_flash_delay > 0:
        time.sleep(args.post_flash_delay)
    run_smoke_test(args)


def provision(args: argparse.Namespace) -> None:
    ensure_target()
    if not args.no_build:
        prepare_build_config()
        run_idf(["build"])
    port = resolve_port(args.port, args.wait_port)
    if args.erase_flash:
        run_idf(["-p", port, "-b", str(args.baud), "erase-flash"])
    run_idf(["-p", port, "-b", str(args.baud), "flash"])

    print("Flashed ESP32 fixture firmware.")
    print(f"Default SoftAP prefix: {args.ap_ssid_prefix}")
    print(f"Default SoftAP password: {args.ap_password or '<open>'}")
    print(f"MCP endpoint: http://{args.host}:{args.http_port}/{args.endpoint}")

    if args.monitor:
        run_idf(["-p", port, "monitor"])
        return

    if not args.smoke:
        print("Run `python3 tools/deploy.py smoke` after connecting to the fixture network.")
        return

    if args.prompt and sys.stdin.isatty():
        input("Connect this computer to the ESP32 fixture Wi-Fi, then press Enter to run the smoke test...")
    elif args.post_flash_delay > 0:
        time.sleep(args.post_flash_delay)
    run_smoke_test(args)


def normalize_flash_map(flash_files: object) -> dict[str, str]:
    if not isinstance(flash_files, dict) or not flash_files:
        raise RuntimeError("Flash file map is missing or empty")
    normalized: dict[str, str] = {}
    for offset, relative_name in flash_files.items():
        normalized[hex(parse_int(offset))] = str(relative_name)
    return normalized


def parse_flash_size(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().upper()
    if text.endswith("MB"):
        return int(text[:-2]) * 1024 * 1024
    if text.endswith("M"):
        return int(text[:-1]) * 1024 * 1024
    try:
        return parse_int(text)
    except ValueError:
        return None


def validate_bundle_dir(bundle_dir: Path) -> dict[str, object]:
    bundle_dir = bundle_dir.resolve()
    manifest_path = bundle_dir / "manifest.json"
    flasher_args_path = bundle_dir / "flasher_args.json"
    flash_args_path = bundle_dir / "flash_args"
    if not manifest_path.exists():
        raise RuntimeError(f"Missing manifest: {manifest_path}")
    if not flasher_args_path.exists():
        raise RuntimeError(f"Missing flasher args: {flasher_args_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        flasher_args = json.loads(flasher_args_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid bundle JSON: {exc}") from exc

    if manifest.get("name") != "ai-hardware-esp32-fixture":
        raise RuntimeError(f"Unexpected bundle name: {manifest.get('name')!r}")
    if manifest.get("target") != "esp32":
        raise RuntimeError(f"Unexpected bundle target: {manifest.get('target')!r}")

    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list) or not manifest_files:
        raise RuntimeError("manifest.json has no files list")

    manifest_flash_files: dict[str, str] = {}
    app_size = None
    app_path = None
    app_offset = None
    for entry in manifest_files:
        if not isinstance(entry, dict):
            raise RuntimeError(f"Invalid manifest file entry: {entry!r}")
        offset = hex(parse_int(entry.get("offset")))
        relative_name = entry.get("path")
        path = safe_bundle_path(bundle_dir, relative_name)
        if not path.exists():
            raise RuntimeError(f"Bundled file is missing: {path}")
        size = path.stat().st_size
        if size != parse_int(entry.get("size")):
            raise RuntimeError(f"Size mismatch for {relative_name}: manifest={entry.get('size')} actual={size}")
        actual_sha256 = sha256_file(path)
        if actual_sha256 != entry.get("sha256"):
            raise RuntimeError(f"SHA-256 mismatch for {relative_name}: manifest={entry.get('sha256')} actual={actual_sha256}")
        manifest_flash_files[offset] = str(relative_name)
        if str(relative_name).endswith(".bin") and Path(str(relative_name)).name == BUILD_BIN.name:
            app_size = size
            app_path = str(relative_name)
            app_offset = parse_int(offset)

    auxiliary_files = manifest.get("auxiliary_files")
    if not isinstance(auxiliary_files, list) or not auxiliary_files:
        raise RuntimeError("manifest.json has no auxiliary_files list")
    auxiliary_paths = set()
    for entry in auxiliary_files:
        if not isinstance(entry, dict):
            raise RuntimeError(f"Invalid auxiliary file entry: {entry!r}")
        relative_name = entry.get("path")
        path = safe_bundle_path(bundle_dir, relative_name)
        if not path.exists():
            raise RuntimeError(f"Bundled auxiliary file is missing: {path}")
        auxiliary_paths.add(str(relative_name))
        size = path.stat().st_size
        if size != parse_int(entry.get("size")):
            raise RuntimeError(f"Size mismatch for {relative_name}: manifest={entry.get('size')} actual={size}")
        actual_sha256 = sha256_file(path)
        if actual_sha256 != entry.get("sha256"):
            raise RuntimeError(f"SHA-256 mismatch for {relative_name}: manifest={entry.get('sha256')} actual={actual_sha256}")

    required_auxiliary = {
        "flash_args",
        "flasher_args.json",
        "flash_command.txt",
        "erase_flash_command.txt",
        "README.md",
    }
    missing_auxiliary = sorted(required_auxiliary - auxiliary_paths)
    if missing_auxiliary:
        raise RuntimeError("manifest.json is missing auxiliary files: " + ", ".join(missing_auxiliary))

    flash_command_text = (bundle_dir / "flash_command.txt").read_text(encoding="utf-8").strip()
    if flash_command_text != manifest.get("flash_command"):
        raise RuntimeError("flash_command.txt does not match manifest flash_command")
    erase_flash_command_text = (bundle_dir / "erase_flash_command.txt").read_text(encoding="utf-8").strip()
    if erase_flash_command_text != manifest.get("erase_flash_command"):
        raise RuntimeError("erase_flash_command.txt does not match manifest erase_flash_command")
    bundle_readme = (bundle_dir / "README.md").read_text(encoding="utf-8", errors="ignore")
    for expected_text in (
        flash_command_text,
        erase_flash_command_text,
        "python3 tools/deploy.py preflight --bundle dist/esp32-fixture.zip",
        "python3 tools/deploy.py verify-bundle --bundle dist/esp32-fixture.zip",
        "python3 tools/deploy.py flash-bundle --bundle <bundle-dir-or-zip>",
        "python3 tools/deploy.py smoke --wait-ready 30",
    ):
        if expected_text not in bundle_readme:
            raise RuntimeError(f"Bundle README.md is missing expected text: {expected_text}")

    flash_args_files = parse_flash_args(flash_args_path)
    if flash_args_files != manifest_flash_files:
        raise RuntimeError(f"flash_args does not match manifest files: {flash_args_files!r} != {manifest_flash_files!r}")

    flasher_files = normalize_flash_map(flasher_args.get("flash_files"))
    if flasher_files != manifest_flash_files:
        raise RuntimeError(f"flasher_args.json does not match manifest files: {flasher_files!r} != {manifest_flash_files!r}")

    app_section = flasher_args.get("app")
    if not isinstance(app_section, dict):
        raise RuntimeError("flasher_args.json has no app section")
    if app_path != app_section.get("file"):
        raise RuntimeError(f"App file mismatch: manifest={app_path!r} flasher_args={app_section.get('file')!r}")

    factory_offset, factory_size = factory_app_partition()
    if app_offset != factory_offset:
        raise RuntimeError(f"App offset {hex(app_offset or 0)} does not match factory partition {hex(factory_offset)}")
    if app_size is None:
        raise RuntimeError("App image entry is missing from manifest")
    if app_size > factory_size:
        raise RuntimeError(f"App image is too large: {app_size} > factory partition {factory_size}")

    flash_size = parse_flash_size(manifest.get("flash_settings", {}).get("flash_size"))
    if flash_size is not None:
        for offset, relative_name in manifest_flash_files.items():
            path = safe_bundle_path(bundle_dir, relative_name)
            end = parse_int(offset) + path.stat().st_size
            if end > flash_size:
                raise RuntimeError(f"{relative_name} exceeds configured flash size: end={hex(end)} flash={hex(flash_size)}")

    sdkconfig = manifest.get("sdkconfig")
    if not isinstance(sdkconfig, dict):
        raise RuntimeError("manifest.json has no sdkconfig object")
    if sdkconfig.get("CONFIG_FIXTURE_MCP_ENDPOINT") != manifest.get("mcp", {}).get("default_endpoint"):
        raise RuntimeError("Manifest MCP endpoint does not match sdkconfig")
    if sdkconfig.get("CONFIG_FIXTURE_ADC_ENABLE") == "y":
        for key in (
            "CONFIG_FIXTURE_ADC_SERIES_MAX_POINTS",
            "CONFIG_FIXTURE_ADC_SERIES_MAX_SAMPLES_PER_POINT",
            "CONFIG_FIXTURE_ADC_SERIES_MAX_INTERVAL_MS",
            "CONFIG_FIXTURE_ADC_SERIES_MAX_TOTAL_MS",
        ):
            if key not in sdkconfig:
                raise RuntimeError(f"Missing ADC series config in manifest: {key}")

    return {
        "bundle_dir": str(bundle_dir),
        "file_count": len(manifest_files),
        "auxiliary_file_count": len(auxiliary_files),
        "app_path": app_path,
        "app_size": app_size,
        "app_offset": hex(app_offset),
        "factory_size": factory_size,
        "factory_free": factory_size - app_size,
    }


def bundle(args: argparse.Namespace) -> None:
    if not args.no_build:
        prepare_build_config()
        run_idf(["build"])
    ensure_build_outputs()

    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    flasher_args = json.loads(FLASHER_ARGS_JSON.read_text(encoding="utf-8"))
    flash_files = flasher_args.get("flash_files")
    if not isinstance(flash_files, dict) or not flash_files:
        raise RuntimeError(f"{FLASHER_ARGS_JSON} has no flash_files section")

    bundled_files: list[dict[str, object]] = []
    for offset, relative_name in sorted(flash_files.items(), key=lambda item: int(item[0], 16)):
        source = BUILD_DIR / relative_name
        if not source.exists():
            raise RuntimeError(f"Flash file referenced by flasher_args.json is missing: {source}")
        destination = output_dir / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        bundled_files.append(
            {
                "offset": offset,
                "path": relative_name,
                "size": destination.stat().st_size,
                "sha256": sha256_file(destination),
            }
        )

    shutil.copy2(FLASH_ARGS, output_dir / "flash_args")
    shutil.copy2(FLASHER_ARGS_JSON, output_dir / "flasher_args.json")

    sdkconfig = read_sdkconfig_values(MANIFEST_CONFIG_KEYS)
    project_description = read_project_description()
    manifest = {
        "name": "ai-hardware-esp32-fixture",
        "target": "esp32",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_dir": str(PROJECT_DIR),
        "project_version": project_description.get("project_version"),
        "idf_version": idf_version_for_manifest(project_description),
        "idf_path": project_description.get("idf_path"),
        "flash_command": flash_command(args.baud),
        "erase_flash_command": erase_flash_command(args.baud),
        "flash_settings": flasher_args.get("flash_settings", {}),
        "files": bundled_files,
        "sdkconfig": sdkconfig,
        "mcp": {
            "default_softap_prefix": sdkconfig.get("CONFIG_FIXTURE_WIFI_AP_SSID_PREFIX", "ai-hardware-fixture"),
            "default_softap_password": sdkconfig.get("CONFIG_FIXTURE_WIFI_AP_PASSWORD", "aihardware"),
            "default_host": DEFAULT_MCP_HOST,
            "default_port": int(sdkconfig.get("CONFIG_FIXTURE_MCP_PORT", DEFAULT_MCP_PORT)),
            "default_endpoint": sdkconfig.get("CONFIG_FIXTURE_MCP_ENDPOINT", DEFAULT_MCP_ENDPOINT),
        },
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "flash_command.txt").write_text(flash_command(args.baud) + "\n", encoding="utf-8")
    (output_dir / "erase_flash_command.txt").write_text(erase_flash_command(args.baud) + "\n", encoding="utf-8")
    (output_dir / "README.md").write_text(
        "\n".join(
            [
                "# AI Hardware ESP32 Fixture Flash Bundle",
                "",
                "This directory contains the ESP32 bootloader, partition table and application image.",
                "Both the direct esptool commands and the source-project helper require a Python environment with esptool installed.",
                "Sourcing ESP-IDF `export.sh` provides that environment.",
                "",
                "From the source project, run a host and bundle readiness check:",
                "",
                "```bash",
                "python3 tools/deploy.py preflight --bundle dist/esp32-fixture.zip",
                "```",
                "",
                "For a clean first bring-up, erase stale NVS/runtime configuration first:",
                "",
                "```bash",
                erase_flash_command(args.baud),
                "```",
                "",
                "Flash from this directory:",
                "",
                "```bash",
                flash_command(args.baud),
                "```",
                "",
                "From the source project, validate this bundle before flashing:",
                "",
                "```bash",
                "python3 tools/deploy.py verify-bundle --bundle dist/esp32-fixture",
                "python3 tools/deploy.py verify-bundle --bundle dist/esp32-fixture.zip",
                "```",
                "",
                "From the source project, flash this verified bundle without rebuilding:",
                "",
                "```bash",
                "python3 tools/deploy.py flash-bundle --bundle <bundle-dir-or-zip> --port /dev/cu.usbserial-XXXX --wait-port 60 --erase-flash",
                "```",
                "",
                "After flashing, connect to the fixture network and run the MCP smoke test from the source project:",
                "",
                "```bash",
                "python3 tools/deploy.py smoke --wait-ready 30",
                "```",
                "",
                "See `manifest.json` for image hashes, offsets and key firmware configuration.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    manifest["auxiliary_files"] = [
        bundle_file_entry(output_dir, relative_name)
        for relative_name in (
            "flash_args",
            "flasher_args.json",
            "flash_command.txt",
            "erase_flash_command.txt",
            "README.md",
        )
    ]
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    archive_path = None
    if args.zip:
        archive_path = shutil.make_archive(str(output_dir), "zip", root_dir=output_dir)

    summary = validate_bundle_dir(output_dir)
    print(f"Bundle written to: {output_dir}")
    print(
        "Bundle verified: "
        f"{summary['file_count']} flash files, {summary['auxiliary_file_count']} auxiliary files, "
        f"app {summary['app_size']} bytes at {summary['app_offset']}, "
        f"{summary['factory_free']} bytes free in factory partition"
    )
    print(f"Flash command: {flash_command(args.baud)}")
    if archive_path:
        print(f"Zip archive: {archive_path}")


def verify_bundle(args: argparse.Namespace) -> None:
    source = args.bundle.resolve()
    with open_bundle_source(args.bundle) as bundle_dir:
        summary = validate_bundle_dir(bundle_dir)
    print(f"OK: flash bundle verified at {source}")
    print(f"Flash files: {summary['file_count']}")
    print(f"Auxiliary files: {summary['auxiliary_file_count']}")
    print(f"App: {summary['app_path']} ({summary['app_size']} bytes at {summary['app_offset']})")
    print(f"Factory partition free: {summary['factory_free']} bytes")


def relative_or_absolute(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(resolved)


def preflight_check(label: str, ok: bool, detail: str, required: bool = True) -> bool:
    if ok:
        status = "OK"
    elif required:
        status = "FAIL"
    else:
        status = "WARN"
    print(f"{status}: {label}: {detail}")
    return ok or not required


def preflight(args: argparse.Namespace) -> None:
    failures = 0
    warnings = 0

    print(f"Project: {PROJECT_DIR}")

    idf = shutil.which("idf.py")
    if not preflight_check(
        "ESP-IDF",
        idf is not None,
        idf or "idf.py not found; source ESP-IDF export.sh before building",
        required=args.require_idf,
    ):
        failures += 1
    elif idf is None:
        warnings += 1

    esptool = esptool_version()
    if not preflight_check(
        "esptool",
        esptool is not None,
        f"{esptool} via {sys.executable}" if esptool else f"not available via {sys.executable}",
    ):
        failures += 1

    if not preflight_check(
        "target",
        target_is_configured(),
        "sdkconfig target is esp32" if target_is_configured() else "sdkconfig is not configured for esp32",
        required=args.require_build,
    ):
        failures += 1
    elif not target_is_configured():
        warnings += 1

    build_outputs = {
        "app": BUILD_BIN,
        "bootloader": BOOTLOADER_BIN,
        "partition_table": PARTITION_BIN,
    }
    for label, path in build_outputs.items():
        if not preflight_check(
            label,
            path.exists(),
            str(path) if path.exists() else f"missing: {path}",
            required=args.require_build,
        ):
            failures += 1
        elif not path.exists():
            warnings += 1

    try:
        with open_bundle_source(args.bundle) as bundle_dir:
            summary = validate_bundle_dir(bundle_dir)
        print(
            "OK: bundle: "
            f"{args.bundle.resolve()} -> app {summary['app_size']} bytes at {summary['app_offset']}, "
            f"{summary['factory_free']} bytes free"
        )
    except RuntimeError as exc:
        print(f"FAIL: bundle: {exc}")
        failures += 1

    port_for_next_step = args.port
    if args.port or args.wait_port > 0:
        try:
            port_for_next_step = resolve_port(args.port, args.wait_port)
            print(f"OK: serial: {port_for_next_step}")
        except RuntimeError as exc:
            required = args.require_port
            if preflight_check("serial", False, str(exc), required=required):
                warnings += 1
            else:
                failures += 1
    else:
        ports_found = discover_ports()
        if len(ports_found) == 1:
            port_for_next_step = ports_found[0]
            print(f"OK: serial: {port_for_next_step}")
        elif len(ports_found) > 1:
            detail = "multiple serial ports found; pass --port explicitly: " + ", ".join(ports_found)
            if preflight_check("serial", False, detail, required=args.require_port):
                warnings += 1
            else:
                failures += 1
        else:
            if preflight_check("serial", False, "none found", required=args.require_port):
                warnings += 1
            else:
                failures += 1

    bundle_arg = relative_or_absolute(args.bundle)
    if port_for_next_step:
        print("Next flash command:")
        print(
            "  "
            f"{sys.executable} {relative_or_absolute(Path(__file__))} flash-bundle "
            f"--bundle {bundle_arg} --port {port_for_next_step} --wait-port 60 "
            "--erase-flash --smoke --prompt --wait-ready 30 --exercise-runtime-net"
        )
    else:
        print("Next: connect an ESP32 serial port, then rerun preflight with --require-port or run flash-bundle.")

    if failures:
        raise RuntimeError(f"Preflight failed with {failures} failure(s) and {warnings} warning(s)")
    print(f"Preflight complete: {warnings} warning(s)")


def ports(_: argparse.Namespace) -> None:
    found = discover_ports()
    if not found:
        print("No serial ports found.")
        return
    for port in found:
        print(port)


def doctor(_: argparse.Namespace) -> None:
    idf = shutil.which("idf.py")
    esptool = esptool_version()
    print(f"Project: {PROJECT_DIR}")
    print(f"idf.py: {idf or 'not found'}")
    print(f"esptool: {esptool or 'not available'} via {sys.executable}")
    print(f"Target configured: {'yes' if target_is_configured() else 'no'}")
    print(f"Build app: {BUILD_BIN if BUILD_BIN.exists() else 'missing'}")
    print(f"Bootloader: {BOOTLOADER_BIN if BOOTLOADER_BIN.exists() else 'missing'}")
    print(f"Partition table: {PARTITION_BIN if PARTITION_BIN.exists() else 'missing'}")

    found = discover_ports()
    if found:
        print("Serial ports:")
        for port in found:
            print(f"  {port}")
    else:
        print("Serial ports: none found")

    if idf is None:
        raise RuntimeError("ESP-IDF environment is not active. Source export.sh before building or flashing.")


def add_port_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", help="Serial port, for example /dev/cu.usbserial-XXXX or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Flash baud rate, default {DEFAULT_BAUD}")
    parser.add_argument(
        "--wait-port",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Wait up to this many seconds for the serial port to appear before failing",
    )


def add_endpoint_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=DEFAULT_MCP_HOST, help=f"MCP host, default {DEFAULT_MCP_HOST}")
    parser.add_argument("--http-port", type=int, default=DEFAULT_MCP_PORT, help=f"MCP HTTP port, default {DEFAULT_MCP_PORT}")
    parser.add_argument("--endpoint", default=DEFAULT_MCP_ENDPOINT, help=f"MCP endpoint path, default {DEFAULT_MCP_ENDPOINT}")
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
        default=DEFAULT_MCP_PROTOCOL_VERSION,
        help=f"MCP protocol version, default {DEFAULT_MCP_PROTOCOL_VERSION}",
    )


def add_smoke_args(parser: argparse.ArgumentParser) -> None:
    add_endpoint_args(parser)
    parser.add_argument(
        "--skip-adc-tool",
        action="store_true",
        help="Do not require fixture.read_adc_raw, useful after disabling ADC in menuconfig",
    )
    parser.add_argument(
        "--exercise-net-adc-tool",
        action="store_true",
        help="Call net ADC tools during smoke test; this changes the MUX selection",
    )
    parser.add_argument(
        "--exercise-runtime-net",
        action="store_true",
        help="Temporarily persist, select and clear a runtime net mapping during smoke test",
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Configure target and build firmware")
    build_parser.set_defaults(func=build)

    flash_parser = subparsers.add_parser("flash", help="Build and flash firmware")
    add_port_args(flash_parser)
    flash_parser.add_argument("--no-build", action="store_true", help="Flash existing build output")
    flash_parser.add_argument("--erase-flash", action="store_true", help="Erase the whole chip before flashing")
    flash_parser.set_defaults(func=flash)

    erase_parser = subparsers.add_parser("erase-flash", help="Erase the whole ESP32 flash")
    add_port_args(erase_parser)
    erase_parser.set_defaults(func=erase_flash)

    monitor_parser = subparsers.add_parser("monitor", help="Open ESP-IDF serial monitor")
    add_port_args(monitor_parser)
    monitor_parser.set_defaults(func=monitor)

    fm_parser = subparsers.add_parser("flash-monitor", help="Build, flash and open monitor")
    add_port_args(fm_parser)
    fm_parser.add_argument("--no-build", action="store_true", help="Flash existing build output")
    fm_parser.add_argument("--erase-flash", action="store_true", help="Erase the whole chip before flashing")
    fm_parser.set_defaults(func=flash_monitor)

    smoke_parser = subparsers.add_parser("smoke", help="Run non-destructive MCP smoke test")
    add_smoke_args(smoke_parser)
    smoke_parser.set_defaults(func=smoke)

    load_map_parser = subparsers.add_parser("load-net-map", help="Load runtime net/testpoint mappings into the fixture")
    add_endpoint_args(load_map_parser)
    mapping_source = load_map_parser.add_mutually_exclusive_group(required=True)
    mapping_source.add_argument(
        "--mappings-json",
        help='Runtime mapping JSON array, for example [{"net":"VIN","channel":0}]',
    )
    mapping_source.add_argument(
        "--mappings-file",
        type=Path,
        help="Path to a JSON file containing an array, or an object with mappings and optional clear_existing",
    )
    load_map_parser.add_argument(
        "--clear-existing",
        action="store_true",
        help="Clear existing runtime mappings before loading the provided mappings",
    )
    load_map_parser.add_argument(
        "--max-channel",
        type=int,
        default=configured_mux_max_channel(),
        help="Maximum allowed MUX channel for local validation; defaults to sdkconfig when available",
    )
    load_map_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the normalized mapping without contacting the ESP32",
    )
    load_map_parser.add_argument(
        "--no-read-back",
        dest="read_back",
        action="store_false",
        help="Do not read fixture://net-map after loading",
    )
    load_map_parser.set_defaults(func=load_net_map, read_back=True)

    provision_parser = subparsers.add_parser("provision", help="Build, flash and optionally run smoke test")
    add_port_args(provision_parser)
    add_smoke_args(provision_parser)
    provision_parser.add_argument("--no-build", action="store_true", help="Flash existing build output")
    provision_parser.add_argument("--erase-flash", action="store_true", help="Erase the whole chip before flashing")
    provision_parser.add_argument("--smoke", action="store_true", help="Run smoke test after flashing")
    provision_parser.add_argument("--monitor", action="store_true", help="Open ESP-IDF monitor after flashing")
    provision_parser.add_argument("--prompt", action="store_true", help="Prompt before smoke test so you can join SoftAP")
    provision_parser.add_argument(
        "--post-flash-delay",
        type=float,
        default=2.0,
        help="Seconds to wait before smoke test when --prompt is not used",
    )
    provision_parser.add_argument(
        "--ap-ssid-prefix",
        default="ai-hardware-fixture",
        help="Displayed SoftAP SSID prefix for operator guidance",
    )
    provision_parser.add_argument(
        "--ap-password",
        default="aihardware",
        help="Displayed SoftAP password for operator guidance",
    )
    provision_parser.set_defaults(func=provision)

    bundle_parser = subparsers.add_parser("bundle", help="Create a distributable esptool flash bundle")
    bundle_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_BUNDLE_DIR,
        help=f"Bundle output directory, default {DEFAULT_BUNDLE_DIR}",
    )
    bundle_parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Flash baud rate in generated command, default {DEFAULT_BAUD}")
    bundle_parser.add_argument("--no-build", action="store_true", help="Bundle existing build output")
    bundle_parser.add_argument("--zip", action="store_true", help="Also create a .zip archive next to the output directory")
    bundle_parser.set_defaults(func=bundle)

    verify_bundle_parser = subparsers.add_parser("verify-bundle", help="Validate an existing esptool flash bundle")
    verify_bundle_parser.add_argument(
        "--bundle",
        type=Path,
        default=DEFAULT_BUNDLE_DIR,
        help=f"Bundle directory or .zip archive to verify, default {DEFAULT_BUNDLE_DIR}",
    )
    verify_bundle_parser.set_defaults(func=verify_bundle)

    flash_bundle_parser = subparsers.add_parser("flash-bundle", help="Verify and flash an existing esptool bundle")
    add_port_args(flash_bundle_parser)
    add_smoke_args(flash_bundle_parser)
    flash_bundle_parser.add_argument(
        "--bundle",
        type=Path,
        default=DEFAULT_BUNDLE_DIR,
        help=f"Bundle directory or .zip archive to flash, default {DEFAULT_BUNDLE_DIR}",
    )
    flash_bundle_parser.add_argument("--erase-flash", action="store_true", help="Erase the whole chip before flashing")
    flash_bundle_parser.add_argument("--smoke", action="store_true", help="Run smoke test after flashing")
    flash_bundle_parser.add_argument("--prompt", action="store_true", help="Prompt before smoke test so you can join SoftAP")
    flash_bundle_parser.add_argument(
        "--post-flash-delay",
        type=float,
        default=2.0,
        help="Seconds to wait before smoke test when --prompt is not used",
    )
    flash_bundle_parser.set_defaults(func=flash_bundle)

    preflight_parser = subparsers.add_parser("preflight", help="Check whether the host and bundle are ready to flash")
    add_port_args(preflight_parser)
    preflight_parser.add_argument(
        "--bundle",
        type=Path,
        default=DEFAULT_BUNDLE_DIR.with_suffix(".zip"),
        help=f"Bundle directory or .zip archive to check, default {DEFAULT_BUNDLE_DIR.with_suffix('.zip')}",
    )
    preflight_parser.add_argument(
        "--require-port",
        action="store_true",
        help="Treat a missing or ambiguous ESP32 serial port as a preflight failure",
    )
    preflight_parser.add_argument(
        "--require-idf",
        action="store_true",
        help="Treat a missing ESP-IDF environment as a preflight failure",
    )
    preflight_parser.add_argument(
        "--require-build",
        action="store_true",
        help="Treat missing local build outputs or target configuration as a preflight failure",
    )
    preflight_parser.set_defaults(func=preflight)

    ports_parser = subparsers.add_parser("ports", help="List candidate ESP32 serial ports")
    ports_parser.set_defaults(func=ports)

    doctor_parser = subparsers.add_parser("doctor", help="Check ESP-IDF, build outputs and serial ports")
    doctor_parser.set_defaults(func=doctor)

    args = parser.parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build, flash and monitor the ESP32 fixture firmware."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
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
DEFAULT_MCP_HOST = "192.168.4.1"
DEFAULT_MCP_PORT = 80
DEFAULT_MCP_ENDPOINT = "mcp"
DEFAULT_MCP_PROTOCOL_VERSION = "2025-03-26"
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
    "CONFIG_FIXTURE_ADC_ENABLE",
    "CONFIG_FIXTURE_ADC_UNIT",
    "CONFIG_FIXTURE_ADC_CHANNEL",
    "CONFIG_FIXTURE_ADC_CALIBRATION_ENABLE",
    "CONFIG_FIXTURE_ADC_DEFAULT_VREF_MV",
    "CONFIG_FIXTURE_ADC_SCALE_NUMERATOR",
    "CONFIG_FIXTURE_ADC_SCALE_DENOMINATOR",
    "CONFIG_FIXTURE_ADC_OFFSET_MV",
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
        "--protocol-version",
        args.protocol_version,
    ]
    if args.skip_adc_tool:
        command.append("--skip-adc-tool")
    if args.exercise_net_adc_tool:
        command.append("--exercise-net-adc-tool")
    subprocess.run(command, cwd=PROJECT_DIR, check=True)


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


def flash_command(baud: int) -> str:
    return (
        f"python -m esptool --chip esp32 -b {baud} --before default_reset --after hard_reset "
        "write_flash @flash_args"
    )


def discover_ports() -> list[str]:
    ports: set[str] = set()
    for pattern in PORT_PATTERNS:
        ports.update(glob.glob(pattern))
    return sorted(ports)


def resolve_port(port: str | None) -> str:
    if port:
        return port

    ports = discover_ports()
    if len(ports) == 1:
        return ports[0]
    if not ports:
        raise RuntimeError("No serial ports found. Pass --port /dev/ttyUSB0 or the macOS /dev/cu.* port.")
    raise RuntimeError("Multiple serial ports found. Pass --port explicitly:\n  " + "\n  ".join(ports))


def build(_: argparse.Namespace) -> None:
    prepare_build_config()
    run_idf(["build"])


def flash(args: argparse.Namespace) -> None:
    ensure_target()
    if not args.no_build:
        prepare_build_config()
        run_idf(["build"])
    port = resolve_port(args.port)
    run_idf(["-p", port, "-b", str(args.baud), "flash"])


def monitor(args: argparse.Namespace) -> None:
    port = resolve_port(args.port)
    run_idf(["-p", port, "monitor"])


def flash_monitor(args: argparse.Namespace) -> None:
    ensure_target()
    if not args.no_build:
        prepare_build_config()
        run_idf(["build"])
    port = resolve_port(args.port)
    run_idf(["-p", port, "-b", str(args.baud), "flash", "monitor"])


def smoke(args: argparse.Namespace) -> None:
    run_smoke_test(args)


def provision(args: argparse.Namespace) -> None:
    ensure_target()
    if not args.no_build:
        prepare_build_config()
        run_idf(["build"])
    port = resolve_port(args.port)
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
    (output_dir / "README.md").write_text(
        "\n".join(
            [
                "# AI Hardware ESP32 Fixture Flash Bundle",
                "",
                "This directory contains the ESP32 bootloader, partition table and application image.",
                "",
                "Flash from this directory:",
                "",
                "```bash",
                flash_command(args.baud),
                "```",
                "",
                "After flashing, connect to the fixture network and run the MCP smoke test from the source project:",
                "",
                "```bash",
                "python3 tools/deploy.py smoke",
                "```",
                "",
                "See `manifest.json` for image hashes, offsets and key firmware configuration.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    archive_path = None
    if args.zip:
        archive_path = shutil.make_archive(str(output_dir), "zip", root_dir=output_dir)

    print(f"Bundle written to: {output_dir}")
    print(f"Flash command: {flash_command(args.baud)}")
    if archive_path:
        print(f"Zip archive: {archive_path}")


def ports(_: argparse.Namespace) -> None:
    found = discover_ports()
    if not found:
        print("No serial ports found.")
        return
    for port in found:
        print(port)


def doctor(_: argparse.Namespace) -> None:
    idf = shutil.which("idf.py")
    print(f"Project: {PROJECT_DIR}")
    print(f"idf.py: {idf or 'not found'}")
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


def add_smoke_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=DEFAULT_MCP_HOST, help=f"MCP host, default {DEFAULT_MCP_HOST}")
    parser.add_argument("--http-port", type=int, default=DEFAULT_MCP_PORT, help=f"MCP HTTP port, default {DEFAULT_MCP_PORT}")
    parser.add_argument("--endpoint", default=DEFAULT_MCP_ENDPOINT, help=f"MCP endpoint path, default {DEFAULT_MCP_ENDPOINT}")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--protocol-version",
        default=DEFAULT_MCP_PROTOCOL_VERSION,
        help=f"MCP protocol version, default {DEFAULT_MCP_PROTOCOL_VERSION}",
    )
    parser.add_argument(
        "--skip-adc-tool",
        action="store_true",
        help="Do not require fixture.read_adc_raw, useful after disabling ADC in menuconfig",
    )
    parser.add_argument(
        "--exercise-net-adc-tool",
        action="store_true",
        help="Call fixture.read_net_adc_raw during smoke test; this changes the MUX selection",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Configure target and build firmware")
    build_parser.set_defaults(func=build)

    flash_parser = subparsers.add_parser("flash", help="Build and flash firmware")
    add_port_args(flash_parser)
    flash_parser.add_argument("--no-build", action="store_true", help="Flash existing build output")
    flash_parser.set_defaults(func=flash)

    monitor_parser = subparsers.add_parser("monitor", help="Open ESP-IDF serial monitor")
    add_port_args(monitor_parser)
    monitor_parser.set_defaults(func=monitor)

    fm_parser = subparsers.add_parser("flash-monitor", help="Build, flash and open monitor")
    add_port_args(fm_parser)
    fm_parser.add_argument("--no-build", action="store_true", help="Flash existing build output")
    fm_parser.set_defaults(func=flash_monitor)

    smoke_parser = subparsers.add_parser("smoke", help="Run non-destructive MCP smoke test")
    add_smoke_args(smoke_parser)
    smoke_parser.set_defaults(func=smoke)

    provision_parser = subparsers.add_parser("provision", help="Build, flash and optionally run smoke test")
    add_port_args(provision_parser)
    add_smoke_args(provision_parser)
    provision_parser.add_argument("--no-build", action="store_true", help="Flash existing build output")
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

    ports_parser = subparsers.add_parser("ports", help="List candidate ESP32 serial ports")
    ports_parser.set_defaults(func=ports)

    doctor_parser = subparsers.add_parser("doctor", help="Check ESP-IDF, build outputs and serial ports")
    doctor_parser.set_defaults(func=doctor)

    args = parser.parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    os.chdir(PROJECT_DIR)
    raise SystemExit(main())

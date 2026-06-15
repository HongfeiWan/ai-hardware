"""Instrument drivers used by the bench prototype."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from math import pi, sin
from pathlib import Path
from typing import Any, Protocol


class PsuDriver(Protocol):
    id: str

    def set_power(self, voltage_v: float, current_limit_a: float, output: bool) -> dict[str, Any]:
        ...

    def status(self) -> dict[str, Any]:
        ...


class ScopeDriver(Protocol):
    id: str

    def capture_waveform(
        self,
        net: str,
        expected_voltage: dict[str, Any] | None,
        symptom: str,
        sample_count: int,
        duration_s: float,
        artifact_path: Path,
    ) -> dict[str, Any]:
        ...

    def capture_screenshot(
        self,
        net: str,
        features: dict[str, Any] | None,
        artifact_path: Path,
    ) -> dict[str, Any]:
        ...

    def status(self) -> dict[str, Any]:
        ...


class DmmDriver(Protocol):
    id: str

    def measure_dc_voltage(
        self,
        net: str,
        expected_voltage: dict[str, Any] | None,
        symptom: str,
    ) -> dict[str, Any]:
        ...

    def measure_impedance(
        self,
        net: str,
        net_info: dict[str, Any],
        symptom: str,
    ) -> dict[str, Any]:
        ...

    def status(self) -> dict[str, Any]:
        ...


class LogicAnalyzerDriver(Protocol):
    id: str

    def capture_logic(
        self,
        net: str,
        symptom: str,
        sample_count: int,
        duration_s: float,
        artifact_path: Path,
    ) -> dict[str, Any]:
        ...

    def status(self) -> dict[str, Any]:
        ...


@dataclass
class MockPsu:
    id: str = "mock_psu_ch1"
    channel: str = "CH1"
    output: bool = False
    voltage_v: float = 0.0
    current_limit_a: float = 0.0

    def set_power(self, voltage_v: float, current_limit_a: float, output: bool) -> dict[str, Any]:
        self.voltage_v = voltage_v
        self.current_limit_a = current_limit_a
        self.output = output
        measured_current = 0.0 if not output else min(current_limit_a * 0.62, current_limit_a)
        return {
            "channel": self.channel,
            "voltage_V": round(voltage_v if output else 0.0, 6),
            "current_limit_A": round(current_limit_a, 6),
            "measured_current_A": round(measured_current, 6),
            "current_limited": bool(output and measured_current >= current_limit_a),
            "output": output,
        }

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "psu",
            "backend": "mock",
            "channel": self.channel,
            "output": self.output,
            "voltage_V": self.voltage_v,
            "current_limit_A": self.current_limit_a,
        }


@dataclass
class MockFixture:
    mux_channel: int | None = None
    last_reset_pulse_ms: int | None = None

    def set_mux(self, channel: int) -> dict[str, Any]:
        self.mux_channel = channel
        return {"ok": True, "mux_channel": channel, "mock": True}

    def reset_dut(self, pulse_ms: int) -> dict[str, Any]:
        self.last_reset_pulse_ms = pulse_ms
        return {"ok": True, "pulse_ms": pulse_ms, "mock": True}


class MockScope:
    id = "mock_scope"

    def capture_waveform(
        self,
        net: str,
        expected_voltage: dict[str, Any] | None,
        symptom: str,
        sample_count: int,
        duration_s: float,
        artifact_path: Path,
    ) -> dict[str, Any]:
        sample_count = max(16, min(sample_count, 20000))
        duration_s = max(1e-6, duration_s)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        samples = self._synthesize(net, expected_voltage, symptom, sample_count, duration_s)
        with artifact_path.open("w", encoding="utf-8") as handle:
            handle.write("t_s,voltage_V\n")
            for t_s, voltage_v in samples:
                handle.write(f"{t_s:.9f},{voltage_v:.6f}\n")
        features = extract_waveform_features(samples)
        return {
            "artifact_path": artifact_path,
            "sample_count": sample_count,
            "duration_s": duration_s,
            "features": features,
        }

    def status(self) -> dict[str, Any]:
        return {"id": self.id, "kind": "oscilloscope", "backend": "mock"}

    def capture_screenshot(
        self,
        net: str,
        features: dict[str, Any] | None,
        artifact_path: Path,
    ) -> dict[str, Any]:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        svg = _mock_scope_screenshot_svg(net, features or {})
        artifact_path.write_text(svg, encoding="utf-8")
        return {
            "artifact_path": artifact_path,
            "mime_type": "image/svg+xml",
            "width_px": 640,
            "height_px": 360,
        }

    def _synthesize(
        self,
        net: str,
        expected_voltage: dict[str, Any] | None,
        symptom: str,
        sample_count: int,
        duration_s: float,
    ) -> list[tuple[float, float]]:
        symptom_l = symptom.lower()
        nominal = 1.0
        if expected_voltage:
            nominal = (float(expected_voltage["min"]) + float(expected_voltage["max"])) / 2.0
        samples: list[tuple[float, float]] = []
        for index in range(sample_count):
            t_s = duration_s * index / max(sample_count - 1, 1)
            phase = t_s / duration_s
            if "does not stay" in symptom_l and net.upper() in {"VOUT_3V3", "3V3"}:
                envelope = min(1.0, phase * 8.0)
                collapse = 1.0 if (phase % 0.42) < 0.28 else 0.18
                voltage = min(nominal * 0.34, nominal) * envelope * collapse
            elif any(token in symptom_l for token in ("overvoltage", "too high", "exceeds")) and expected_voltage:
                voltage = float(expected_voltage["max"]) * 1.18 + nominal * 0.01 * sin(2 * pi * 4 * phase)
            elif "ripple" in symptom_l:
                voltage = nominal + nominal * 0.06 * sin(2 * pi * 12 * phase)
            elif net.upper().endswith("SW_NODE"):
                if any(token in symptom_l for token in ("not switching", "stuck low", "no switching", "no pulses")):
                    voltage = 0.02 * nominal * sin(2 * pi * 3 * phase)
                elif any(token in symptom_l for token in ("stuck high", "always high")):
                    voltage = nominal
                else:
                    voltage = nominal * (1.0 if sin(2 * pi * 24 * phase) > 0 else 0.0)
            else:
                voltage = nominal + nominal * 0.004 * sin(2 * pi * 5 * phase)
            samples.append((t_s, round(voltage, 6)))
        return samples


class MockDmm:
    id = "mock_dmm"

    def measure_dc_voltage(
        self,
        net: str,
        expected_voltage: dict[str, Any] | None,
        symptom: str,
    ) -> dict[str, Any]:
        voltage = self._synthesize_voltage(net, expected_voltage, symptom)
        features: dict[str, Any] = {"voltage_V": voltage}
        if expected_voltage:
            minimum = float(expected_voltage["min"])
            maximum = float(expected_voltage["max"])
            features.update(
                {
                    "expected_min_V": minimum,
                    "expected_max_V": maximum,
                    "below_expected": voltage < minimum,
                    "above_expected": voltage > maximum,
                    "within_expected": minimum <= voltage <= maximum,
                    "margin_to_min_V": round(voltage - minimum, 6),
                    "margin_to_max_V": round(maximum - voltage, 6),
                }
            )
        return {
            "result": {
                "voltage_V": voltage,
                "unit": "V",
                "mode": "dc_voltage",
            },
            "features": features,
        }

    def measure_impedance(
        self,
        net: str,
        net_info: dict[str, Any],
        symptom: str,
    ) -> dict[str, Any]:
        resistance_ohm = self._synthesize_impedance(net, net_info, symptom)
        features = {
            "resistance_ohm": resistance_ohm,
            "short_to_ground": resistance_ohm < 10.0,
            "low_impedance": resistance_ohm < 100.0,
            "open_like": resistance_ohm > 1_000_000.0,
        }
        return {
            "result": {
                "resistance_ohm": resistance_ohm,
                "unit": "ohm",
                "mode": "resistance_2w",
            },
            "features": features,
        }

    def status(self) -> dict[str, Any]:
        return {"id": self.id, "kind": "dmm", "backend": "mock", "modes": ["dc_voltage", "resistance_2w"]}

    def _synthesize_voltage(
        self,
        net: str,
        expected_voltage: dict[str, Any] | None,
        symptom: str,
    ) -> float:
        symptom_l = symptom.lower()
        net_u = net.upper()
        if net_u == "GND":
            return 0.0
        if expected_voltage:
            minimum = float(expected_voltage["min"])
            maximum = float(expected_voltage["max"])
            nominal = (minimum + maximum) / 2.0
            if any(token in symptom_l for token in ("overvoltage", "too high", "exceeds")):
                return round(maximum * 1.12, 6)
            if any(token in symptom_l for token in ("enable low", "en low", "not enabled", "disabled")) and (
                net_u.startswith("EN") or "_EN" in net_u
            ):
                return 0.0
            if any(token in symptom_l for token in ("does not stay", "collapse", "brownout")) and net_u in {
                "VOUT_3V3",
                "3V3",
            }:
                return round(minimum * 0.35, 6)
            if (
                any(token in symptom_l for token in ("ldo output", "ldo abnormal", "1v8 low", "1.8v low"))
                and net_u in {"VDD_1V8", "1V8", "LDO_OUT"}
            ):
                return round(minimum * 0.22, 6)
            if any(token in symptom_l for token in ("power good low", "pg low", "pgood low")) and net_u.startswith("PG"):
                return 0.0
            return round(nominal, 6)
        if "EN" in net_u or "PG" in net_u:
            return 3.3
        return 0.0

    def _synthesize_impedance(self, net: str, net_info: dict[str, Any], symptom: str) -> float:
        symptom_l = symptom.lower()
        net_u = net.upper()
        if net_u == "GND":
            return 0.08
        if any(token in symptom_l for token in ("short", "shorted", "overcurrent")) and net_info.get("domain") == "power":
            return 1.4
        if net_info.get("domain") == "power":
            return 47_000.0
        if net_info.get("domain") == "digital":
            return 100_000.0
        return 1_000_000.0


class MockLogicAnalyzer:
    id = "mock_logic_analyzer"

    def capture_logic(
        self,
        net: str,
        symptom: str,
        sample_count: int,
        duration_s: float,
        artifact_path: Path,
    ) -> dict[str, Any]:
        sample_count = max(8, min(sample_count, 20000))
        duration_s = max(1e-6, duration_s)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        samples = self._synthesize(net, symptom, sample_count, duration_s)
        with artifact_path.open("w", encoding="utf-8") as handle:
            handle.write("t_s,level\n")
            for t_s, level in samples:
                handle.write(f"{t_s:.9f},{level}\n")
        return {
            "artifact_path": artifact_path,
            "sample_count": sample_count,
            "duration_s": duration_s,
            "features": extract_logic_features(samples),
        }

    def status(self) -> dict[str, Any]:
        return {"id": self.id, "kind": "logic_analyzer", "backend": "mock", "channels": ["D0", "D1", "D2", "D3"]}

    def _synthesize(self, net: str, symptom: str, sample_count: int, duration_s: float) -> list[tuple[float, int]]:
        symptom_l = symptom.lower()
        net_u = net.upper()
        stuck_low = (
            any(token in symptom_l for token in ("power good low", "pg low", "pgood low")) and net_u.startswith("PG")
        ) or (
            any(token in symptom_l for token in ("enable low", "en low", "not enabled", "disabled"))
            and (net_u.startswith("EN") or "_EN" in net_u)
        ) or (
            any(token in symptom_l for token in ("reset stuck", "reset low", "not released", "held in reset"))
            and ("RESET" in net_u or "RST" in net_u or "NRST" in net_u)
        ) or (
            any(
                token in symptom_l
                for token in (
                    "clock missing",
                    "clock not",
                    "no clock",
                    "not oscillating",
                    "no oscillation",
                    "crystal",
                    "oscillator",
                )
            )
            and ("CLK" in net_u or "CLOCK" in net_u or "XTAL" in net_u or "OSC" in net_u)
        ) or (
            any(
                token in symptom_l
                for token in (
                    "bus held low",
                    "bus stuck low",
                    "held low",
                    "stuck low",
                    "i2c",
                    "spi",
                    "scl low",
                    "sda low",
                )
            )
            and (
                net_u.startswith(("I2C_", "SPI_"))
                or net_u.endswith(("_SCL", "_SDA", "_SCK", "_MOSI", "_MISO", "_CS"))
                or net_u in {"SCL", "SDA", "SCK", "MOSI", "MISO", "CS"}
            )
        )
        samples: list[tuple[float, int]] = []
        for index in range(sample_count):
            t_s = duration_s * index / max(sample_count - 1, 1)
            if stuck_low:
                level = 0
            elif any(token in symptom_l for token in ("toggling", "clock", "pulsing")):
                level = 1 if (index // 4) % 2 == 0 else 0
            else:
                level = 1
            samples.append((t_s, level))
        return samples


def extract_waveform_features(samples: list[tuple[float, float]]) -> dict[str, Any]:
    if not samples:
        raise ValueError("Cannot extract features from empty waveform")
    values = [voltage for _, voltage in samples]
    v_min = min(values)
    v_max = max(values)
    v_avg = sum(values) / len(values)
    v_pp = v_max - v_min
    first = values[0]
    last = values[-1]
    settles = abs(last - v_avg) <= max(0.03, abs(v_avg) * 0.05)
    return {
        "v_min_V": round(v_min, 6),
        "v_max_V": round(v_max, 6),
        "v_avg_V": round(v_avg, 6),
        "v_pp_V": round(v_pp, 6),
        "first_V": round(first, 6),
        "last_V": round(last, 6),
        "settles": settles,
    }


def extract_logic_features(samples: list[tuple[float, int]]) -> dict[str, Any]:
    if not samples:
        raise ValueError("Cannot extract features from empty logic capture")
    levels = [int(level) for _, level in samples]
    high_count = sum(1 for level in levels if level)
    low_count = len(levels) - high_count
    transitions = sum(1 for left, right in zip(levels, levels[1:]) if left != right)
    return {
        "sample_count": len(samples),
        "high_count": high_count,
        "low_count": low_count,
        "high_fraction": round(high_count / len(levels), 6),
        "low_fraction": round(low_count / len(levels), 6),
        "transition_count": transitions,
        "first_level": levels[0],
        "last_level": levels[-1],
        "stuck_high": high_count == len(levels),
        "stuck_low": low_count == len(levels),
    }


class ScpiConnection:
    """Tiny PyVISA wrapper, imported lazily so mock mode has no dependency."""

    def __init__(self, resource: str, timeout_ms: int = 5000) -> None:
        try:
            import pyvisa  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("PyVISA is required for SCPI instruments. Install pyvisa and a VISA backend.") from exc
        self.resource = resource
        self.rm = pyvisa.ResourceManager()
        self.instrument = self.rm.open_resource(resource)
        self.instrument.timeout = timeout_ms

    def write(self, command: str) -> None:
        self.instrument.write(command)

    def query(self, command: str) -> str:
        return str(self.instrument.query(command)).strip()

    def query_ascii_values(self, command: str) -> list[float]:
        values = self.instrument.query_ascii_values(command)
        return [float(value) for value in values]

    def query_raw_bytes(self, command: str) -> bytes:
        self.instrument.write(command)
        return bytes(self.instrument.read_raw())


@dataclass
class ScpiPsu:
    resource: str
    channel: str = "CH1"
    id: str = "scpi_psu"
    timeout_ms: int = 5000
    voltage_command: str = "SOURce:VOLTage {voltage}"
    current_command: str = "SOURce:CURRent {current}"
    output_command: str = "OUTPut {state}"
    channel_select_command: str | None = "INSTrument:NSELect {channel_index}"
    measure_voltage_query: str = "MEASure:VOLTage?"
    measure_current_query: str = "MEASure:CURRent?"

    def __post_init__(self) -> None:
        self.connection = ScpiConnection(self.resource, self.timeout_ms)

    def set_power(self, voltage_v: float, current_limit_a: float, output: bool) -> dict[str, Any]:
        self._select_channel()
        self.connection.write(self.voltage_command.format(voltage=voltage_v, current=current_limit_a, state=int(output)))
        self.connection.write(self.current_command.format(voltage=voltage_v, current=current_limit_a, state=int(output)))
        self.connection.write(self.output_command.format(voltage=voltage_v, current=current_limit_a, state=int(output)))
        measured_voltage = _safe_float_query(self.connection, self.measure_voltage_query)
        measured_current = _safe_float_query(self.connection, self.measure_current_query)
        return {
            "channel": self.channel,
            "backend": "scpi",
            "resource": self.resource,
            "voltage_V": measured_voltage if measured_voltage is not None else voltage_v,
            "current_limit_A": current_limit_a,
            "measured_current_A": measured_current,
            "current_limited": measured_current is not None and measured_current >= current_limit_a * 0.98,
            "output": output,
        }

    def status(self) -> dict[str, Any]:
        return {"id": self.id, "kind": "psu", "backend": "scpi", "resource": self.resource, "channel": self.channel}

    def _select_channel(self) -> None:
        if self.channel_select_command:
            channel_index = "".join(ch for ch in self.channel if ch.isdigit()) or self.channel
            self.connection.write(self.channel_select_command.format(channel=self.channel, channel_index=channel_index))


@dataclass
class ScpiScope:
    resource: str
    channel: str = "CHANnel1"
    id: str = "scpi_scope"
    timeout_ms: int = 10000
    waveform_source_command: str = "WAVeform:SOURce {channel}"
    waveform_points_command: str = "WAVeform:POINts {points}"
    waveform_data_query: str = "WAVeform:DATA?"
    sample_interval_s: float | None = None
    screenshot_command: str = "DISPlay:DATA? PNG"

    def __post_init__(self) -> None:
        self.connection = ScpiConnection(self.resource, self.timeout_ms)

    def capture_waveform(
        self,
        net: str,
        expected_voltage: dict[str, Any] | None,
        symptom: str,
        sample_count: int,
        duration_s: float,
        artifact_path: Path,
    ) -> dict[str, Any]:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection.write(self.waveform_source_command.format(channel=self.channel, points=sample_count))
        self.connection.write(self.waveform_points_command.format(channel=self.channel, points=sample_count))
        values = self.connection.query_ascii_values(self.waveform_data_query)
        if not values:
            raise RuntimeError("SCPI scope returned no waveform samples")
        interval = self.sample_interval_s or (duration_s / max(len(values) - 1, 1))
        samples = [(index * interval, value) for index, value in enumerate(values)]
        with artifact_path.open("w", encoding="utf-8") as handle:
            handle.write("t_s,voltage_V\n")
            for t_s, voltage_v in samples:
                handle.write(f"{t_s:.9f},{voltage_v:.6f}\n")
        return {
            "artifact_path": artifact_path,
            "sample_count": len(samples),
            "duration_s": interval * max(len(samples) - 1, 1),
            "features": extract_waveform_features(samples),
        }

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": "oscilloscope",
            "backend": "scpi",
            "resource": self.resource,
            "channel": self.channel,
        }

    def capture_screenshot(
        self,
        net: str,
        features: dict[str, Any] | None,
        artifact_path: Path,
    ) -> dict[str, Any]:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.connection.query_raw_bytes(self.screenshot_command)
        if not data:
            raise RuntimeError("SCPI scope returned no screenshot data")
        artifact_path.write_bytes(data)
        return {
            "artifact_path": artifact_path,
            "mime_type": "image/png",
            "width_px": None,
            "height_px": None,
        }


@dataclass
class ScpiDmm:
    resource: str
    id: str = "scpi_dmm"
    timeout_ms: int = 5000
    dc_voltage_query: str = "MEASure:VOLTage:DC?"
    resistance_query: str = "MEASure:RESistance?"

    def __post_init__(self) -> None:
        self.connection = ScpiConnection(self.resource, self.timeout_ms)

    def measure_dc_voltage(
        self,
        net: str,
        expected_voltage: dict[str, Any] | None,
        symptom: str,
    ) -> dict[str, Any]:
        voltage = _safe_float_query(self.connection, self.dc_voltage_query)
        if voltage is None:
            raise RuntimeError("SCPI DMM returned no DC voltage value")
        features: dict[str, Any] = {"voltage_V": voltage}
        if expected_voltage:
            minimum = float(expected_voltage["min"])
            maximum = float(expected_voltage["max"])
            features.update(
                {
                    "expected_min_V": minimum,
                    "expected_max_V": maximum,
                    "below_expected": voltage < minimum,
                    "above_expected": voltage > maximum,
                    "within_expected": minimum <= voltage <= maximum,
                    "margin_to_min_V": round(voltage - minimum, 6),
                    "margin_to_max_V": round(maximum - voltage, 6),
                }
            )
        return {
            "result": {"voltage_V": voltage, "unit": "V", "mode": "dc_voltage"},
            "features": features,
        }

    def measure_impedance(
        self,
        net: str,
        net_info: dict[str, Any],
        symptom: str,
    ) -> dict[str, Any]:
        resistance = _safe_float_query(self.connection, self.resistance_query)
        if resistance is None:
            raise RuntimeError("SCPI DMM returned no resistance value")
        return {
            "result": {"resistance_ohm": resistance, "unit": "ohm", "mode": "resistance_2w"},
            "features": {
                "resistance_ohm": resistance,
                "short_to_ground": resistance < 10.0,
                "low_impedance": resistance < 100.0,
                "open_like": resistance > 1_000_000.0,
            },
        }

    def status(self) -> dict[str, Any]:
        return {"id": self.id, "kind": "dmm", "backend": "scpi", "resource": self.resource}


def build_psu_driver(config: dict[str, Any] | None) -> PsuDriver:
    if not config or config.get("backend", "mock") == "mock":
        return MockPsu(id=config.get("id", "mock_psu_ch1") if config else "mock_psu_ch1")
    if config.get("backend") == "scpi":
        return ScpiPsu(
            resource=str(config["resource"]),
            channel=str(config.get("channel", "CH1")),
            id=str(config.get("id", "scpi_psu")),
            timeout_ms=int(config.get("timeout_ms", 5000)),
        )
    raise ValueError(f"Unsupported PSU backend: {config.get('backend')}")


def build_scope_driver(config: dict[str, Any] | None) -> ScopeDriver:
    if not config or config.get("backend", "mock") == "mock":
        scope = MockScope()
        scope.id = config.get("id", "mock_scope") if config else "mock_scope"
        return scope
    if config.get("backend") == "scpi":
        return ScpiScope(
            resource=str(config["resource"]),
            channel=str(config.get("channel", "CHANnel1")),
            id=str(config.get("id", "scpi_scope")),
            timeout_ms=int(config.get("timeout_ms", 10000)),
            sample_interval_s=config.get("sample_interval_s"),
        )
    raise ValueError(f"Unsupported scope backend: {config.get('backend')}")


def build_dmm_driver(config: dict[str, Any] | None) -> DmmDriver:
    if not config or config.get("backend", "mock") == "mock":
        dmm = MockDmm()
        dmm.id = config.get("id", "mock_dmm") if config else "mock_dmm"
        return dmm
    if config.get("backend") == "scpi":
        return ScpiDmm(
            resource=str(config["resource"]),
            id=str(config.get("id", "scpi_dmm")),
            timeout_ms=int(config.get("timeout_ms", 5000)),
        )
    raise ValueError(f"Unsupported DMM backend: {config.get('backend')}")


def build_logic_analyzer_driver(config: dict[str, Any] | None) -> LogicAnalyzerDriver:
    if not config or config.get("backend", "mock") == "mock":
        logic = MockLogicAnalyzer()
        logic.id = config.get("id", "mock_logic_analyzer") if config else "mock_logic_analyzer"
        return logic
    raise ValueError(f"Unsupported logic analyzer backend: {config.get('backend')}")


def _safe_float_query(connection: ScpiConnection, query: str) -> float | None:
    try:
        return float(connection.query(query))
    except Exception:
        return None


def _mock_scope_screenshot_svg(net: str, features: dict[str, Any]) -> str:
    width = 640
    height = 360
    grid = []
    for x in range(40, width, 80):
        grid.append(f'<line x1="{x}" y1="40" x2="{x}" y2="310" stroke="#203040" stroke-width="1"/>')
    for y in range(40, 320, 54):
        grid.append(f'<line x1="40" y1="{y}" x2="600" y2="{y}" stroke="#203040" stroke-width="1"/>')
    v_avg = float(features.get("v_avg_V", 1.0) or 1.0)
    v_pp = float(features.get("v_pp_V", 0.2) or 0.2)
    points = []
    for index in range(160):
        phase = index / 159
        x = 40 + phase * 560
        y = 175 - 70 * (0.55 * sin(2 * pi * 2.8 * phase) + 0.15 * sin(2 * pi * 17 * phase))
        points.append(f"{x:.1f},{y:.1f}")
    safe_net = escape(net)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">'
        '<rect width="640" height="360" fill="#0b1118"/>'
        + "".join(grid)
        + f'<polyline points="{" ".join(points)}" fill="none" stroke="#33d17a" stroke-width="3"/>'
        f'<text x="40" y="28" fill="#e5edf5" font-family="Menlo, monospace" font-size="18">Mock Scope - {safe_net}</text>'
        f'<text x="40" y="338" fill="#9fb0c0" font-family="Menlo, monospace" font-size="14">avg={v_avg:.4g} V  vpp={v_pp:.4g} V</text>'
        '</svg>'
    )

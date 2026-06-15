"""Instrument drivers used by the bench prototype."""

from __future__ import annotations

from dataclasses import dataclass
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
            elif "ripple" in symptom_l:
                voltage = nominal + nominal * 0.06 * sin(2 * pi * 12 * phase)
            elif net.upper().endswith("SW_NODE"):
                voltage = nominal * (1.0 if sin(2 * pi * 24 * phase) > 0 else 0.0)
            else:
                voltage = nominal + nominal * 0.004 * sin(2 * pi * 5 * phase)
            samples.append((t_s, round(voltage, 6)))
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


def _safe_float_query(connection: ScpiConnection, query: str) -> float | None:
    try:
        return float(connection.query(query))
    except Exception:
        return None

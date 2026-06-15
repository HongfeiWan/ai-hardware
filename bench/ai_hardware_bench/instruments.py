"""Mock instrument drivers used by the first bench prototype."""

from __future__ import annotations

from dataclasses import dataclass
from math import pi, sin
from pathlib import Path
from typing import Any


@dataclass
class MockPsu:
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


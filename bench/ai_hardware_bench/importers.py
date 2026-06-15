"""Board context importers for common early-stage source files."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any

from .data import SCHEMA_VERSION, BoardContext


def import_board(
    source: str | Path,
    source_format: str,
    board_id: str,
    board_name: str,
    output: str | Path,
) -> dict[str, Any]:
    if source_format == "csv":
        context = import_testpoint_csv(source, board_id, board_name)
    elif source_format in {"kicad", "kicad-xml"}:
        context = import_kicad_xml_netlist(source, board_id, board_name)
    else:
        raise ValueError(f"Unsupported import format: {source_format}")
    BoardContext(context, source_path=Path(source))
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(context, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(target), "board_id": board_id, "counts": _counts(context)}


def import_testpoint_csv(source: str | Path, board_id: str, board_name: str) -> dict[str, Any]:
    nets: dict[str, dict[str, Any]] = {}
    components: dict[str, dict[str, Any]] = {}
    test_points: list[dict[str, Any]] = []
    with Path(source).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_no, row in enumerate(reader, start=2):
            net_name = _first(row, "net", "net_name", "name")
            if not net_name:
                raise ValueError(f"CSV row {row_no} is missing net/net_name")
            net = nets.setdefault(
                net_name,
                {
                    "name": net_name,
                    "domain": row.get("domain") or "unknown",
                    "risk_level": row.get("risk_level") or "low",
                },
            )
            aliases = _split_list(row.get("aliases", ""))
            if aliases:
                net["aliases"] = aliases
            expected_min = row.get("expected_voltage_min") or row.get("voltage_min")
            expected_max = row.get("expected_voltage_max") or row.get("voltage_max")
            if expected_min and expected_max:
                net["expected_voltage"] = {
                    "min": float(expected_min),
                    "max": float(expected_max),
                    "unit": row.get("voltage_unit") or "V",
                }
            point_id = _first(row, "test_point", "tp", "id")
            if point_id:
                point: dict[str, Any] = {
                    "id": point_id,
                    "net": net_name,
                    "label": row.get("label") or point_id,
                    "probe_hint": row.get("probe_hint") or "",
                    "risk_level": row.get("risk_level") or net.get("risk_level", "low"),
                }
                measurements = _split_list(row.get("allowed_measurements", ""))
                if measurements:
                    point["allowed_measurements"] = measurements
                test_points.append(point)
            designator = _first(row, "component", "designator", "ref")
            pin = _first(row, "pin", "pin_name")
            if designator and pin:
                component = components.setdefault(
                    designator,
                    {
                        "designator": designator,
                        "type": row.get("component_type") or row.get("type") or "unknown",
                        "value": row.get("value") or "",
                        "pins": [],
                    },
                )
                component["pins"].append({"name": pin, "net": net_name, "function": row.get("pin_function") or ""})
    if not components:
        components["J1"] = {
            "designator": "J1",
            "type": "test_header",
            "pins": [{"name": name, "net": name} for name in nets],
        }
    return _context(board_id, board_name, source, list(nets.values()), list(components.values()), test_points)


def import_kicad_xml_netlist(source: str | Path, board_id: str, board_name: str) -> dict[str, Any]:
    tree = ET.parse(source)
    root = tree.getroot()
    components: dict[str, dict[str, Any]] = {}
    for comp in root.findall(".//components/comp"):
        ref = comp.attrib.get("ref")
        if not ref:
            continue
        components[ref] = {
            "designator": ref,
            "type": "component",
            "value": _child_text(comp, "value") or "",
            "part_number": _child_text(comp, "libsource/part") or "",
            "package": _child_text(comp, "footprint") or "",
            "pins": [],
        }
    nets: dict[str, dict[str, Any]] = {}
    for net in root.findall(".//nets/net"):
        net_name = net.attrib.get("name") or f"net_{net.attrib.get('code', 'unknown')}"
        nets.setdefault(net_name, {"name": net_name, "domain": _guess_domain(net_name), "risk_level": "low"})
        for node in net.findall("node"):
            ref = node.attrib.get("ref")
            pin = node.attrib.get("pin")
            if not ref or not pin:
                continue
            component = components.setdefault(ref, {"designator": ref, "type": "component", "pins": []})
            component.setdefault("pins", []).append({"name": pin, "net": net_name})
    test_points = []
    for ref, component in components.items():
        if ref.upper().startswith(("TP", "TEST")) and component.get("pins"):
            first_pin = component["pins"][0]
            test_points.append(
                {
                    "id": ref,
                    "net": first_pin["net"],
                    "label": ref,
                    "allowed_measurements": ["dc_voltage", "waveform", "logic"],
                    "risk_level": nets[first_pin["net"]].get("risk_level", "low"),
                }
            )
    return _context(board_id, board_name, source, list(nets.values()), list(components.values()), test_points)


def _context(
    board_id: str,
    board_name: str,
    source: str | Path,
    nets: list[dict[str, Any]],
    components: list[dict[str, Any]],
    test_points: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "board": {
            "id": board_id,
            "name": board_name,
            "source_files": [str(source)],
        },
        "nets": sorted(nets, key=lambda item: item["name"]),
        "components": sorted(components, key=lambda item: item["designator"]),
        "test_points": sorted(test_points, key=lambda item: item["id"]),
    }


def _counts(context: dict[str, Any]) -> dict[str, int]:
    return {
        "nets": len(context.get("nets", [])),
        "components": len(context.get("components", [])),
        "test_points": len(context.get("test_points", [])),
    }


def _first(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return value.strip()
    return ""


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def _child_text(element: ET.Element, path: str) -> str:
    child = element.find(path)
    return child.text.strip() if child is not None and child.text else ""


def _guess_domain(name: str) -> str:
    upper = name.upper()
    if upper in {"GND", "GNDA", "DGND", "AGND"}:
        return "ground"
    if upper.startswith(("VCC", "VBUS", "VIN", "VOUT", "+", "3V3", "5V")):
        return "power"
    return "unknown"


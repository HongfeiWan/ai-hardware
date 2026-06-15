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
    elif source_format in {"altium", "altium-csv", "altium-tsv"}:
        context = import_altium_csv_netlist(source, board_id, board_name)
    elif source_format in {"bom", "bom-csv", "bom-tsv"}:
        context = import_bom_csv(source, board_id, board_name)
    elif source_format in {"pnp", "pick-place", "pick-and-place", "pickplace"}:
        context = import_pick_place_csv(source, board_id, board_name)
    elif source_format in {"kicad", "kicad-xml"}:
        if Path(source).suffix.lower() in {".kicad_pcb", ".kicad_sch"}:
            context = import_kicad_sexpr(source, board_id, board_name)
        else:
            context = import_kicad_xml_netlist(source, board_id, board_name)
    elif source_format in {"kicad-sexpr", "kicad-pcb", "kicad-sch"}:
        context = import_kicad_sexpr(source, board_id, board_name)
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


def import_bom_csv(source: str | Path, board_id: str, board_name: str) -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {}
    with Path(source).open("r", encoding="utf-8", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel_tab if Path(source).suffix.lower() in {".tsv", ".tab"} else csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("BOM file is missing a header row")
        for row_no, row in enumerate(reader, start=2):
            refs = _split_refs(_first(row, "designator", "ref", "references", "reference", "refs"))
            if not refs:
                raise ValueError(f"BOM row {row_no} is missing designator/ref/references")
            for ref in refs:
                components[ref] = {
                    "designator": ref,
                    "type": _first(row, "component_type", "type", "category") or "bom_item",
                    "value": _first(row, "value", "comment", "description"),
                    "part_number": _first(row, "part_number", "mpn", "manufacturer_part_number", "part"),
                    "package": _first(row, "package", "footprint", "pcb_footprint"),
                    "pins": [],
                }
    if not components:
        raise ValueError("BOM file did not contain any components")
    nets = [{"name": "UNASSIGNED", "domain": "unknown", "risk_level": "low", "notes": "BOM import has no netlist data."}]
    return _context(board_id, board_name, source, nets, list(components.values()), [])


def import_pick_place_csv(source: str | Path, board_id: str, board_name: str) -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {}
    with Path(source).open("r", encoding="utf-8", newline="") as handle:
        reader = _dict_reader(handle, source)
        if not reader.fieldnames:
            raise ValueError("Pick-and-place file is missing a header row")
        for row_no, row in enumerate(reader, start=2):
            ref = _first(row, "designator", "ref", "reference", "component")
            if not ref:
                raise ValueError(f"Pick-and-place row {row_no} is missing designator/ref")
            component: dict[str, Any] = {
                "designator": ref,
                "type": _first(row, "component_type", "type", "category") or "pnp_item",
                "value": _first(row, "comment", "value", "description"),
                "part_number": _first(row, "part_number", "mpn", "manufacturer_part_number", "part"),
                "package": _first(row, "footprint", "package", "pcb_footprint"),
                "pins": [],
            }
            x = _first(row, "center-x(mm)", "center x", "center-x", "x", "mid x", "pos x")
            y = _first(row, "center-y(mm)", "center y", "center-y", "y", "mid y", "pos y")
            if x and y:
                component["placement"] = {
                    "x": float(x),
                    "y": float(y),
                    "unit": _first(row, "unit", "units") or "mm",
                    "rotation_deg": float(_first(row, "rotation", "rotation(deg)", "rot") or 0.0),
                    "side": _first(row, "layer", "side", "designator layer") or "unknown",
                }
            components[ref] = component
    if not components:
        raise ValueError("Pick-and-place file did not contain any components")
    nets = [{"name": "UNASSIGNED", "domain": "unknown", "risk_level": "low", "notes": "Pick-and-place import has no netlist data."}]
    return _context(board_id, board_name, source, nets, list(components.values()), [])


def import_altium_csv_netlist(source: str | Path, board_id: str, board_name: str) -> dict[str, Any]:
    nets: dict[str, dict[str, Any]] = {}
    components: dict[str, dict[str, Any]] = {}
    test_points: dict[str, dict[str, Any]] = {}
    with Path(source).open("r", encoding="utf-8", newline="") as handle:
        reader = _dict_reader(handle, source)
        if not reader.fieldnames:
            raise ValueError("Altium CSV file is missing a header row")
        for row_no, row in enumerate(reader, start=2):
            net_name = _first(row, "net_name", "net", "net label", "netlabel", "signal")
            if not net_name:
                raise ValueError(f"Altium CSV row {row_no} is missing Net/Net Name")
            net = nets.setdefault(net_name, {"name": net_name, "domain": _guess_domain(net_name), "risk_level": "low"})
            designator = _first(
                row,
                "component designator",
                "component",
                "designator",
                "refdes",
                "ref",
                "reference",
            )
            pin = _first(row, "pin designator", "pin number", "pin name", "pin", "pad designator", "pad")
            if designator:
                component = components.setdefault(
                    designator,
                    {
                        "designator": designator,
                        "type": _first(row, "component type", "type", "category") or "component",
                        "value": _first(row, "comment", "value", "description"),
                        "part_number": _first(row, "part number", "mpn", "manufacturer part number", "part"),
                        "package": _first(row, "footprint", "package", "pcb footprint"),
                        "pins": [],
                    },
                )
                if pin:
                    component["pins"].append(
                        {"name": pin, "net": net_name, "function": _first(row, "pin function", "function")}
                    )
                if _looks_like_test_point(designator):
                    test_points.setdefault(
                        designator,
                        {
                            "id": designator,
                            "net": net_name,
                            "label": _first(row, "label", "test point label") or designator,
                            "allowed_measurements": ["dc_voltage", "waveform", "logic"],
                            "risk_level": net.get("risk_level", "low"),
                        },
                    )
            point_id = _first(row, "test point", "testpoint", "test point designator", "tp")
            if point_id:
                test_points.setdefault(
                    point_id,
                    {
                        "id": point_id,
                        "net": net_name,
                        "label": _first(row, "label", "test point label") or point_id,
                        "allowed_measurements": _split_list(_first(row, "allowed measurements")) or [
                            "dc_voltage",
                            "waveform",
                            "logic",
                        ],
                        "risk_level": net.get("risk_level", "low"),
                    },
                )
    if not nets:
        raise ValueError("Altium CSV file did not contain any nets")
    if not components:
        components["J1"] = {
            "designator": "J1",
            "type": "imported_header",
            "pins": [{"name": name, "net": name} for name in nets],
        }
    return _context(board_id, board_name, source, list(nets.values()), list(components.values()), list(test_points.values()))


def import_kicad_sexpr(source: str | Path, board_id: str, board_name: str) -> dict[str, Any]:
    root = _parse_sexpr(Path(source).read_text(encoding="utf-8"))
    if not isinstance(root, list) or not root:
        raise ValueError("KiCad S-expression file did not contain a root list")
    if root[0] == "kicad_pcb":
        return _import_kicad_pcb_sexpr(root, source, board_id, board_name)
    if root[0] == "kicad_sch":
        return _import_kicad_sch_sexpr(root, source, board_id, board_name)
    raise ValueError(f"Unsupported KiCad S-expression root: {root[0]}")


def _import_kicad_pcb_sexpr(root: list[Any], source: str | Path, board_id: str, board_name: str) -> dict[str, Any]:
    nets: dict[str, dict[str, Any]] = {}
    components: dict[str, dict[str, Any]] = {}
    test_points: dict[str, dict[str, Any]] = {}
    for item in _sexp_children(root, "net"):
        if len(item) >= 3:
            name = str(item[2])
            if not name:
                continue
            nets.setdefault(name, {"name": name, "domain": _guess_domain(name), "risk_level": "low"})
    for footprint in _sexp_children(root, "footprint"):
        package = str(footprint[1]) if len(footprint) > 1 else ""
        properties = _sexp_properties(footprint)
        ref = properties.get("Reference") or properties.get("REFERENCE") or _fp_text(footprint, "reference")
        if not ref:
            continue
        value = properties.get("Value") or properties.get("VALUE") or _fp_text(footprint, "value")
        component: dict[str, Any] = {
            "designator": ref,
            "type": "test_point" if _looks_like_test_point(ref) else "component",
            "value": value,
            "part_number": properties.get("Part Number", "") or properties.get("MPN", ""),
            "package": properties.get("Footprint", "") or package,
            "pins": [],
        }
        at = _first_child(footprint, "at")
        if at and len(at) >= 3:
            component["placement"] = {
                "x": float(at[1]),
                "y": float(at[2]),
                "unit": "mm",
                "rotation_deg": float(at[3]) if len(at) >= 4 and _is_number(at[3]) else 0.0,
                "side": _footprint_side(footprint),
            }
        for pad in _sexp_children(footprint, "pad"):
            if len(pad) < 2:
                continue
            net_item = _first_child(pad, "net")
            if not net_item or len(net_item) < 3:
                continue
            net_name = str(net_item[2])
            nets.setdefault(net_name, {"name": net_name, "domain": _guess_domain(net_name), "risk_level": "low"})
            component["pins"].append({"name": str(pad[1]), "net": net_name})
        components[ref] = component
        if _looks_like_test_point(ref) and component["pins"]:
            first_pin = component["pins"][0]
            test_points[ref] = {
                "id": ref,
                "net": first_pin["net"],
                "label": ref,
                "allowed_measurements": ["dc_voltage", "waveform", "logic"],
                "risk_level": nets[first_pin["net"]].get("risk_level", "low"),
            }
    if not nets:
        nets["UNASSIGNED"] = {
            "name": "UNASSIGNED",
            "domain": "unknown",
            "risk_level": "low",
            "notes": "KiCad PCB import did not expose net assignments.",
        }
    if not components:
        raise ValueError("KiCad PCB file did not contain any footprints")
    return _context(board_id, board_name, source, list(nets.values()), list(components.values()), list(test_points.values()))


def _import_kicad_sch_sexpr(root: list[Any], source: str | Path, board_id: str, board_name: str) -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {}
    for symbol in _sexp_children(root, "symbol"):
        properties = _sexp_properties(symbol)
        ref = properties.get("Reference") or properties.get("REFERENCE")
        if not ref:
            continue
        components[ref] = {
            "designator": ref,
            "type": "component",
            "value": properties.get("Value", ""),
            "part_number": properties.get("Part Number", "") or properties.get("MPN", ""),
            "package": properties.get("Footprint", ""),
            "pins": [],
        }
    if not components:
        raise ValueError("KiCad schematic file did not contain any symbols with Reference properties")
    nets = [{"name": "UNASSIGNED", "domain": "unknown", "risk_level": "low", "notes": "KiCad schematic import has no resolved netlist data."}]
    return _context(board_id, board_name, source, nets, list(components.values()), [])


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


def _parse_sexpr(text: str) -> list[Any]:
    tokens = _sexpr_tokens(text)
    stack: list[list[Any]] = []
    root: list[Any] | None = None
    for token in tokens:
        if token == "(":
            new_list: list[Any] = []
            if stack:
                stack[-1].append(new_list)
            stack.append(new_list)
            if root is None:
                root = new_list
        elif token == ")":
            if not stack:
                raise ValueError("Unexpected ')' in S-expression")
            stack.pop()
        elif not stack:
            raise ValueError("Atom outside root S-expression")
        else:
            stack[-1].append(token)
    if stack:
        raise ValueError("Unclosed S-expression list")
    if root is None:
        raise ValueError("Empty S-expression file")
    return root


def _sexpr_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == ";":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char in "()":
            tokens.append(char)
            index += 1
            continue
        if char == '"':
            index += 1
            value: list[str] = []
            while index < len(text):
                current = text[index]
                if current == "\\" and index + 1 < len(text):
                    value.append(text[index + 1])
                    index += 2
                    continue
                if current == '"':
                    index += 1
                    break
                value.append(current)
                index += 1
            tokens.append("".join(value))
            continue
        start = index
        while index < len(text) and not text[index].isspace() and text[index] not in "();":
            index += 1
        tokens.append(text[start:index])
    return tokens


def _sexp_children(node: list[Any], head: str) -> list[list[Any]]:
    return [item for item in node if isinstance(item, list) and item and item[0] == head]


def _first_child(node: list[Any], head: str) -> list[Any] | None:
    for item in node:
        if isinstance(item, list) and item and item[0] == head:
            return item
    return None


def _sexp_properties(node: list[Any]) -> dict[str, str]:
    properties: dict[str, str] = {}
    for item in _sexp_children(node, "property"):
        if len(item) >= 3:
            properties[str(item[1])] = str(item[2])
    return properties


def _fp_text(footprint: list[Any], kind: str) -> str:
    for item in _sexp_children(footprint, "fp_text"):
        if len(item) >= 3 and item[1] == kind:
            return str(item[2])
    return ""


def _footprint_side(footprint: list[Any]) -> str:
    layer = _first_child(footprint, "layer")
    if not layer or len(layer) < 2:
        return "unknown"
    value = str(layer[1])
    return "Bottom" if value.startswith("B.") else "Top" if value.startswith("F.") else value


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _dict_reader(handle: Any, source: str | Path) -> csv.DictReader:
    sample = handle.read(4096)
    handle.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel_tab if Path(source).suffix.lower() in {".tsv", ".tab"} else csv.excel
    return csv.DictReader(handle, dialect=dialect)


def _first(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return value.strip()
    normalized = {_normalize_key(key): value for key, value in row.items() if key is not None}
    for key in keys:
        value = normalized.get(_normalize_key(key))
        if value:
            return value.strip()
    return ""


def _normalize_key(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def _split_refs(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace(";", ",")
    refs: list[str] = []
    for chunk in normalized.split(","):
        refs.extend(item.strip() for item in chunk.split() if item.strip())
    return refs


def _child_text(element: ET.Element, path: str) -> str:
    child = element.find(path)
    return child.text.strip() if child is not None and child.text else ""


def _looks_like_test_point(designator: str) -> bool:
    upper = designator.upper()
    return upper.startswith(("TP", "TEST"))


def _guess_domain(name: str) -> str:
    upper = name.upper()
    if upper in {"GND", "GNDA", "DGND", "AGND"}:
        return "ground"
    if upper.startswith(("VCC", "VBUS", "VIN", "VOUT", "+", "3V3", "5V")):
        return "power"
    return "unknown"

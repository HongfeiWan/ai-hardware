"""Data loading and lightweight validation for bench-side diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


SCHEMA_VERSION = "0.1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_document(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() in {".json"}:
        data = json.loads(text)
    elif source.suffix.lower() in {".yaml", ".yml"}:
        data = parse_yaml_subset(text)
    else:
        raise ValueError(f"Unsupported file type for {source}; use JSON or YAML")
    if not isinstance(data, dict):
        raise ValueError(f"{source} did not contain an object")
    return data


def parse_yaml_subset(text: str) -> Any:
    """Parse the small YAML subset used by repository examples.

    This fallback keeps the prototype dependency-free. It supports nested
    mappings, lists, quoted scalars and inline scalar arrays.
    """

    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        return _YamlSubsetParser(text).parse()
    loaded = yaml.safe_load(text)
    return loaded


class _YamlSubsetParser:
    def __init__(self, text: str) -> None:
        self.entries: list[tuple[int, str, int]] = []
        for line_no, raw in enumerate(text.splitlines(), start=1):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
                raise ValueError(f"Tabs are not supported in YAML indentation at line {line_no}")
            indent = len(raw) - len(raw.lstrip(" "))
            self.entries.append((indent, raw.strip(), line_no))

    def parse(self) -> Any:
        if not self.entries:
            return {}
        value, index = self._parse_block(0, self.entries[0][0])
        if index != len(self.entries):
            _, _, line_no = self.entries[index]
            raise ValueError(f"Unexpected YAML content at line {line_no}")
        return value

    def _parse_block(self, index: int, indent: int) -> tuple[Any, int]:
        if index >= len(self.entries):
            return {}, index
        current_indent, text, line_no = self.entries[index]
        if current_indent != indent:
            raise ValueError(f"Unexpected indentation at line {line_no}")
        if text.startswith("- "):
            return self._parse_list(index, indent)
        return self._parse_mapping(index, indent)

    def _parse_list(self, index: int, indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while index < len(self.entries):
            current_indent, text, line_no = self.entries[index]
            if current_indent < indent:
                break
            if current_indent != indent or not text.startswith("- "):
                break
            body = text[2:].strip()
            index += 1
            if not body:
                value, index = self._parse_block(index, self.entries[index][0])
                items.append(value)
                continue
            if self._looks_like_pair(body):
                key, rest = self._split_pair(body, line_no)
                item: dict[str, Any] = {}
                if rest:
                    item[key] = self._parse_scalar(rest)
                elif index < len(self.entries) and self.entries[index][0] > indent:
                    item[key], index = self._parse_block(index, self.entries[index][0])
                else:
                    item[key] = None
                if index < len(self.entries) and self.entries[index][0] > indent:
                    extra, index = self._parse_mapping(index, self.entries[index][0])
                    item.update(extra)
                items.append(item)
            else:
                items.append(self._parse_scalar(body))
        return items, index

    def _parse_mapping(self, index: int, indent: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while index < len(self.entries):
            current_indent, text, line_no = self.entries[index]
            if current_indent < indent:
                break
            if current_indent != indent or text.startswith("- "):
                break
            key, rest = self._split_pair(text, line_no)
            index += 1
            if rest:
                mapping[key] = self._parse_scalar(rest)
            elif index < len(self.entries) and self.entries[index][0] > indent:
                mapping[key], index = self._parse_block(index, self.entries[index][0])
            else:
                mapping[key] = None
        return mapping, index

    @staticmethod
    def _looks_like_pair(text: str) -> bool:
        return re.match(r"^[A-Za-z0-9_.-]+:\s*", text) is not None

    @staticmethod
    def _split_pair(text: str, line_no: int) -> tuple[str, str]:
        if ":" not in text:
            raise ValueError(f"Expected key/value pair at line {line_no}")
        key, rest = text.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Empty key at line {line_no}")
        return key, rest.strip()

    def _parse_scalar(self, value: str) -> Any:
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                return []
            return [self._parse_scalar(part.strip()) for part in inner.split(",")]
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            return value[1:-1]
        if value in {"true", "True"}:
            return True
        if value in {"false", "False"}:
            return False
        if value in {"null", "None", "~"}:
            return None
        try:
            if re.match(r"^-?\d+$", value):
                return int(value)
            if re.match(r"^-?\d+\.\d+([eE]-?\d+)?$", value):
                return float(value)
        except ValueError:
            pass
        return value


@dataclass
class BoardContext:
    data: dict[str, Any]
    source_path: Path | None = None
    nets: dict[str, dict[str, Any]] = field(init=False)
    aliases: dict[str, str] = field(init=False)
    components: dict[str, dict[str, Any]] = field(init=False)
    test_points: dict[str, dict[str, Any]] = field(init=False)
    rails: dict[str, dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        self.validate()
        self.nets = {net["name"]: net for net in self.data.get("nets", [])}
        self.aliases = {}
        for name, net in self.nets.items():
            for alias in net.get("aliases", []) or []:
                self.aliases[alias] = name
        self.components = {component["designator"]: component for component in self.data.get("components", [])}
        self.test_points = {point["id"]: point for point in self.data.get("test_points", [])}
        self.rails = {rail["name"]: rail for rail in self.data.get("rails", []) or []}

    @property
    def board_id(self) -> str:
        return str(self.data["board"]["id"])

    def canonical_net(self, name: str) -> str:
        if name in self.nets:
            return name
        if name in self.aliases:
            return self.aliases[name]
        raise ValueError(f"Unknown net: {name}")

    def validate(self) -> None:
        if self.data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported schema_version {self.data.get('schema_version')!r}")
        for key in ("board", "nets", "components", "test_points"):
            if key not in self.data:
                raise ValueError(f"Board context missing required field: {key}")
        board = self.data["board"]
        if not isinstance(board, dict) or not board.get("id") or not board.get("name"):
            raise ValueError("Board context board must contain id and name")
        nets = self.data["nets"]
        if not isinstance(nets, list) or not nets:
            raise ValueError("Board context must contain at least one net")
        net_names: set[str] = set()
        for net in nets:
            name = net.get("name")
            if not name:
                raise ValueError("Every net needs a name")
            if name in net_names:
                raise ValueError(f"Duplicate net name: {name}")
            net_names.add(name)
        component_names: set[str] = set()
        for component in self.data["components"]:
            designator = component.get("designator")
            if not designator or not component.get("type"):
                raise ValueError("Every component needs designator and type")
            if designator in component_names:
                raise ValueError(f"Duplicate component designator: {designator}")
            component_names.add(designator)
            for pin in component.get("pins", []):
                if not pin.get("name") or not pin.get("net"):
                    raise ValueError(f"Component {designator} has an incomplete pin")
                if pin["net"] not in net_names:
                    raise ValueError(f"Pin {designator}.{pin.get('name')} references unknown net {pin['net']}")
        test_point_ids: set[str] = set()
        for point in self.data.get("test_points", []):
            point_id = point.get("id")
            if not point_id or not point.get("net"):
                raise ValueError("Every test point needs id and net")
            if point_id in test_point_ids:
                raise ValueError(f"Duplicate test point id: {point_id}")
            test_point_ids.add(point_id)
            if point["net"] not in net_names:
                raise ValueError(f"Test point {point_id} references unknown net {point['net']}")
        rail_names: set[str] = set()
        for rail in self.data.get("rails", []) or []:
            rail_name = rail.get("name")
            if not rail_name or not rail.get("source_net") or not rail.get("output_net"):
                raise ValueError("Every rail needs name, source_net and output_net")
            if rail_name in rail_names:
                raise ValueError(f"Duplicate rail name: {rail_name}")
            rail_names.add(rail_name)
            if rail["output_net"] not in net_names:
                raise ValueError(f"Rail {rail_name} references unknown output_net {rail['output_net']}")
            for key in ("enable_net", "power_good_net"):
                if rail.get(key) and rail[key] not in net_names:
                    raise ValueError(f"Rail {rail_name} references unknown {key} {rail[key]}")
        for constraint in self.data.get("constraints", []) or []:
            constraint_type = constraint.get("type")
            if constraint_type in {"do_not_drive", "manual_confirmation"}:
                for target in constraint.get("targets", []) or []:
                    if target not in net_names:
                        raise ValueError(f"Constraint {constraint.get('id')} references unknown net target {target}")


def load_board_context(path: str | Path) -> BoardContext:
    source = Path(path)
    return BoardContext(load_document(source), source_path=source)


@dataclass
class DiagnosticSession:
    board_id: str
    observed_symptom: str
    session_id: str = "session_mock_001"
    operator: str = "bench"
    data: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.data = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "board_id": self.board_id,
            "started_at": utc_now(),
            "operator": self.operator,
            "observed_symptom": self.observed_symptom,
            "instruments": [
                {
                    "id": "mock_psu_ch1",
                    "kind": "psu",
                    "vendor": "ai-hardware",
                    "model": "MockPSU",
                    "channels": ["CH1"],
                },
                {
                    "id": "mock_scope",
                    "kind": "oscilloscope",
                    "vendor": "ai-hardware",
                    "model": "MockScope",
                    "channels": ["CH1", "CH2", "CH3", "CH4"],
                },
                {
                    "id": "mock_dmm",
                    "kind": "dmm",
                    "vendor": "ai-hardware",
                    "model": "MockDMM",
                },
                {
                    "id": "mock_logic_analyzer",
                    "kind": "logic_analyzer",
                    "vendor": "ai-hardware",
                    "model": "MockLogicAnalyzer",
                    "channels": ["D0", "D1", "D2", "D3"],
                },
                {
                    "id": "mock_fixture",
                    "kind": "esp32_fixture",
                    "vendor": "ai-hardware",
                    "model": "MockFixture",
                },
            ],
            "measurements": [],
            "findings": [],
            "next_actions": [],
            "artifacts": [],
        }

    def add_measurement(self, measurement: dict[str, Any]) -> dict[str, Any]:
        self.data["measurements"].append(measurement)
        return measurement

    def add_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        self.data["artifacts"].append(artifact)
        return artifact

    def add_finding(self, finding: dict[str, Any]) -> dict[str, Any]:
        self.data["findings"].append(finding)
        return finding

    def set_next_actions(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.data["next_actions"] = actions
        return actions

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return target

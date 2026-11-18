"""装置类型表（XML）模型。

支持节点：
    MeasureDefine  遥测  FUN=数据区起始字节, INF=字节长度, Factor=系数
    SignalDefine   遥信  FUN=数据区起始字节, INF=位号
    ControlDefine  遥控  GroupNo=功能码, EntryNo=子类型
    FixValDefine   定值  FUN=起始字节, INF=字节长度
    SetPointDefine 定值（兼容旧表）

每个 (GroupNo, EntryNo) 对应一个组，组内维护一份 bytearray：
它既是 UI 上的点值来源，也是模拟器回复的数据区。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import xml.etree.ElementTree as ET

KIND_NAMES = {
    "YC": "遥测",
    "YX": "遥信",
    "YK": "遥控",
    "FIX": "定值",
    "MIX": "混合",
    "RAW": "模板",
}


def parse_hex(text: str) -> bytes:
    """解析 "00 18 12 5C" / "0x0018" / "0018" 等写法。"""
    s = text.replace("0x", "").replace("0X", "")
    s = s.replace(",", " ").replace(";", " ").replace("-", " ")
    s = "".join(ch for ch in s if ch not in " \t\r\n")
    if not s:
        return b""
    if len(s) % 2:
        s = "0" + s
    return bytes.fromhex(s)


def format_hex(data: bytes) -> str:
    return data.hex(" ").upper()


@dataclass
class Point:
    kind: str
    name: str
    group_no: int
    entry_no: int
    fun: int
    inf: int
    factor: float = 1.0
    ied_dot: str = ""
    value_type: str = ""
    enable: bool = True

    @property
    def key(self) -> tuple[int, int]:
        return (self.group_no, self.entry_no)

    @property
    def fun_type(self) -> int:
        return (self.group_no << 8) | self.entry_no

    @property
    def end(self) -> int:
        if self.kind == "YK":
            return 0
        if self.kind == "YX":
            return self.fun + 1
        return self.fun + self.inf


@dataclass
class Group:
    group_no: int
    entry_no: int
    kind: str = "YC"
    points: list[Point] = field(default_factory=list)
    data: bytearray = field(default_factory=bytearray)
    result: int = 1  # 遥控/配置应答结果：0x01 成功，0x81 失败

    @property
    def key(self) -> tuple[int, int]:
        return (self.group_no, self.entry_no)

    @property
    def fun_type(self) -> int:
        return (self.group_no << 8) | self.entry_no

    @property
    def title(self) -> str:
        kind = KIND_NAMES.get(self.kind, self.kind)
        first = self.points[0].name if self.points else "无点定义"
        return f"0x{self.fun_type:04X}  {kind}  {first}"


class DeviceModel:
    def __init__(self) -> None:
        self.groups: dict[tuple[int, int], Group] = {}
        self.xml_path: Path | None = None

    def clear(self) -> None:
        self.groups.clear()
        self.xml_path = None

    @staticmethod
    def _decode(raw: bytes) -> str:
        for enc in ("gb18030", "gb2312", "utf-8-sig", "utf-8"):
            try:
                return raw.decode(enc).lstrip("\ufeff")
            except UnicodeDecodeError:
                continue
        return raw.decode("gb18030", errors="replace").lstrip("\ufeff")

    @staticmethod
    def _enabled(el: ET.Element) -> bool:
        return (el.get("Enable", "1") or "1").strip() != "0"

    @staticmethod
    def _int_attr(el: ET.Element, name: str, default: int = 0) -> int:
        try:
            return int((el.get(name) or "").strip() or default)
        except ValueError:
            return default

    @staticmethod
    def _float_attr(el: ET.Element, name: str, default: float = 1.0) -> float:
        try:
            return float((el.get(name) or "").strip() or default)
        except ValueError:
            return default

    def load_xml(self, path: str | Path) -> None:
        path = Path(path)
        text = self._decode(path.read_bytes())
        root = ET.fromstring(text)

        groups: dict[tuple[int, int], Group] = {}
        sections = (
            ("MeasureDefine", "YC"),
            ("SignalDefine", "YX"),
            ("ControlDefine", "YK"),
            ("FixValDefine", "FIX"),
            ("SetPointDefine", "FIX"),
        )

        for section_name, kind in sections:
            section = root.find(section_name)
            if section is None:
                continue
            for el in list(section):
                if not self._enabled(el):
                    continue
                group_no = self._int_attr(el, "GroupNo")
                entry_no = self._int_attr(el, "EntryNo")
                if group_no <= 0 or entry_no <= 0:
                    continue

                fun = self._int_attr(el, "FUN")
                inf = self._int_attr(el, "INF")
                name = (el.get("Name") or "").strip()
                ied_dot = (el.get("IEDDot") or "").strip()
                value_type = (el.get("ValueType") or el.get("Type") or "").strip()
                factor = self._float_attr(el, "Factor", 1.0)
                if kind == "FIX":
                    transcoe = self._float_attr(el, "Transcoe", 1.0)
                    if factor == 1.0:
                        factor = transcoe

                point = Point(
                    kind=kind,
                    name=name,
                    group_no=group_no,
                    entry_no=entry_no,
                    fun=fun,
                    inf=inf,
                    factor=factor,
                    ied_dot=ied_dot,
                    value_type=value_type,
                )

                key = (group_no, entry_no)
                group = groups.get(key)
                if group is None:
                    group = Group(group_no, entry_no, kind)
                    groups[key] = group
                elif group.kind != kind:
                    group.kind = "MIX"
                group.points.append(point)

        self.groups = groups
        self.xml_path = path
        self._finalize()

    def _finalize(self) -> None:
        for group in self.groups.values():
            ends = [p.end for p in group.points]
            length = max(ends) if ends else 0
            if group.kind == "YK":
                length = max(length, 1)
            group.data = bytearray(length)
            if group.kind == "YK" and group.data:
                group.data[0] = group.result
            group.points.sort(key=lambda p: (p.fun, p.inf, p.name))

    @staticmethod
    def xml_end(group: Group) -> int:
        ends = [p.end for p in group.points]
        return max(ends) if ends else 0

    def find(self, group_no: int, entry_no: int) -> Group | None:
        return self.groups.get((group_no, entry_no))

    def point_counts(self) -> dict[str, int]:
        """按点类型统计 XML 中的有效点数量。"""
        counts = {"YC": 0, "YX": 0, "YK": 0, "FIX": 0}
        for group in self.groups.values():
            for point in group.points:
                if point.kind in counts:
                    counts[point.kind] += 1
        return counts

    def sorted_groups(self) -> list[Group]:
        return [self.groups[key] for key in sorted(self.groups)]

    @staticmethod
    def parse_group_key(text: str) -> tuple[int, int]:
        """支持 "0x1B01"、"1B01"、"27/1"、"0x1B,0x01"。"""
        s = text.strip().lower().replace("0x", "")
        if "/" in s:
            a, b = s.split("/", 1)
            return int(a, 16), int(b, 16)
        if "," in s or ":" in s:
            a, b = re.split(r"[,:]", s, 1)
            return int(a, 16), int(b, 16)
        value = int(s, 16)
        return (value >> 8) & 0xFF, value & 0xFF

    def apply_templates(self, templates: dict[str, str]) -> None:
        for key_text, hex_text in templates.items():
            try:
                group_no, entry_no = self.parse_group_key(key_text)
                data = parse_hex(hex_text)
            except (ValueError, TypeError):
                continue

            key = (group_no, entry_no)
            group = self.groups.get(key)
            if group is None:
                group = Group(group_no, entry_no, "RAW")
                self.groups[key] = group

            size = max(len(group.data), len(data))
            new_data = bytearray(size)
            new_data[: len(group.data)] = group.data
            new_data[: len(data)] = data
            group.data = new_data
            if group.kind == "YK" and data:
                group.result = data[0]

    def reset_defaults(self, templates: dict[str, str]) -> None:
        for group in self.groups.values():
            group.data = bytearray(len(group.data))
            group.result = 1
            if group.kind == "YK" and group.data:
                group.data[0] = 1
        self.apply_templates(templates)
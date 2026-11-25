"""协议出厂默认值（原始值）。

默认值来自各型号规格书，不使用实物抓包。
值按 XML 的原始数据区写入，系数换算仍由点格式/转发表处理。
"""

from __future__ import annotations

from app.device_model import Group, Point
from app.point_format import PointFormat
from app.point_format_store import point_key

# 通用默认值（原始值）
COMMON_DEFAULTS: dict[str, float] = {
    "电池容量": 24.0,
    "活化放电截止电压": 4700.0,
    "寿命监测放电截止电压": 4700.0,
    "电池剩余报警容量": 0.0,
    "电池剩余容量": 100.0,
    "活化间隔天数": 0.0,
    "寿命监测间隔天数": 0.0,
    "活化间隔时间": 0.0,
    "寿命监测间隔时间": 0.0,
    "均充浮充间隔时间": 30.0,
    "均充间隔时间": 30.0,
    "温度告警值": 0.0,
    "活化结束时间": 0.0,
    "寿命监测结束时间": 0.0,
    "内阻测试间隔": 0.0,
    "绝缘测试周期": 0.0,
}

# 型号默认值（原始值）；电池容量单位为 Ah，电压类为 0.01V。
MODEL_DEFAULTS: dict[str, dict[str, float]] = {
    "WTJND": {
        "电池容量": 24.0,
        "活化放电截止电压": 4700.0,
        "电池剩余报警容量": 0.0,
    },
    "CL": {
        "电池容量": 20.0,
        "活化放电截止电压": 4800.0,
        "电池剩余报警容量": 0.0,
        "活化间隔天数": 0.0,
        "温度告警值": 0.0,
        "活化结束时间": 0.0,
        "内阻测试间隔": 0.0,
        "活化间隔时间": 0.0,
    },
    "FJ": {
        "电池容量": 15.0,
        "寿命监测放电截止电压": 4500.0,
        "电池剩余报警容量": 0.0,
        "最长活化时间限制": 1440.0,
    },
    "SC": {
        "电池容量": 24.0,
        "活化放电截止电压": 4700.0,
        "电池剩余报警容量": 20.0,
        "活化间隔天数": 0.0,
        "活化间隔时间": 0.0,
        "内阻测试间隔": 0.0,
    },
    "CQ800": {
        "电池容量": 12.0,
        "活化放电截止电压": 4600.0,
        "电池剩余报警容量": 0.0,
        "活化间隔天数": 90.0,
    },
}

# 已知组的完整协议布局；用于补齐 XML 未定义、但协议应答必须有的字段。
GROUP_LAYOUTS: dict[int, list[tuple[str, float]]] = {
    0x1B01: [
        ("电池容量", 24.0),
        ("活化放电截止电压", 4700.0),
        ("电池剩余报警容量", 0.0),
        ("电池剩余容量", 100.0),
    ],
}


def detect_model(xml_stem: str) -> str:
    name = xml_stem.upper()
    if "WTJND" in name:
        return "WTJND"
    if "CN1000" in name or "FJ" in name:
        return "FJ"
    if "CQ800" in name or "WD48" in name:
        return "CQ800"
    if "SC" in name:
        return "SC"
    if "CL" in name or "DLSR" in name:
        return "CL"
    return ""


def defaults_for_xml(xml_stem: str) -> dict[str, float]:
    defaults = dict(COMMON_DEFAULTS)
    defaults.update(MODEL_DEFAULTS.get(detect_model(xml_stem), {}))
    return defaults


def _write_raw(
    group: Group,
    point: Point,
    raw_value: float,
    formats: dict[str, PointFormat],
) -> None:
    if point.kind != "FIX" or point.inf <= 0:
        return
    fmt = formats.get(point_key(point), PointFormat())
    try:
        data = fmt.encode_raw(raw_value, point.inf)
    except (ValueError, OverflowError):
        return
    end = point.fun + point.inf
    if end > len(group.data):
        group.data.extend(b"\x00" * (end - len(group.data)))
    group.data[point.fun : end] = data


def apply_group_defaults(
    group: Group,
    defaults: dict[str, float],
    formats: dict[str, PointFormat] | None = None,
) -> None:
    """按协议布局和点名称写入出厂默认值。"""
    formats = formats or {}

    layout = GROUP_LAYOUTS.get(group.fun_type)
    if layout:
        size = max(len(group.data), len(layout) * 2)
        if size > len(group.data):
            group.data.extend(b"\x00" * (size - len(group.data)))
        for index, (name, fallback) in enumerate(layout):
            value = defaults.get(name, fallback)
            raw = int(round(value))
            group.data[index * 2 : index * 2 + 2] = raw.to_bytes(2, "big")

    for point in group.points:
        value = defaults.get(point.name)
        if value is not None:
            _write_raw(group, point, value, formats)

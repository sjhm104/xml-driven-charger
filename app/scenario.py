"""场景时间线：按时间设置点名或整组数据区。

JSON 示例见 data/scenario_example.json：
{
  "name": "AC loss",
  "loop": true,
  "steps": [
    {"at": 0, "log": "初始", "set": {"0x0101": {"电池电压": 53.5}}},
    {"at": 3000, "log": "A 相失电", "set": {"0x0102": {"A 相失电告警信号": true}}}
  ]
}

`at` 单位毫秒；也可以写 `at_s`（秒）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import struct

from app.device_model import DeviceModel, parse_hex
from app.point_format import PointFormat
from app.point_format_store import point_key


@dataclass
class ScenarioStep:
    at_ms: int
    log: str = ""
    set_points: dict[str, dict[str, object]] = field(default_factory=dict)
    set_group_data: dict[str, str] = field(default_factory=dict)


@dataclass
class Scenario:
    name: str
    loop: bool
    steps: list[ScenarioStep]


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("场景文件不是 JSON 对象")

    raw_steps = obj.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("场景文件缺少 steps 数组")

    steps: list[ScenarioStep] = []
    for index, item in enumerate(raw_steps, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个 step 不是对象")
        if "at_s" in item:
            at_ms = int(float(item["at_s"]) * 1000)
        else:
            at_ms = int(item.get("at", 0))
        if at_ms < 0:
            raise ValueError(f"第 {index} 个 step 的 at 不能为负")

        set_points = item.get("set", item.get("set_points", {}))
        if not isinstance(set_points, dict):
            raise ValueError(f"第 {index} 个 step 的 set 必须是对象")

        group_data = item.get("group_data", item.get("set_group_data", {}))
        if not isinstance(group_data, dict):
            raise ValueError(f"第 {index} 个 step 的 group_data 必须是对象")

        steps.append(
            ScenarioStep(
                at_ms=at_ms,
                log=str(item.get("log", "")),
                set_points=set_points,
                set_group_data={str(k): str(v) for k, v in group_data.items()},
            )
        )

    steps.sort(key=lambda step: step.at_ms)
    return Scenario(
        name=str(obj.get("name", path.stem)),
        loop=bool(obj.get("loop", False)),
        steps=steps,
    )


def apply_step(
    device: DeviceModel,
    step: ScenarioStep,
    formats: dict[str, PointFormat] | None = None,
) -> tuple[int, list[str]]:
    formats = formats or {}
    updated = 0
    notes: list[str] = []

    for key, payload_hex in step.set_group_data.items():
        try:
            group_no, entry_no = DeviceModel.parse_group_key(key)
            payload = parse_hex(payload_hex)
        except (ValueError, TypeError) as exc:
            notes.append(f"{key}: {exc}")
            continue
        group = device.find(group_no, entry_no)
        if group is None:
            notes.append(f"{key}: 当前 XML 没有这个组")
            continue
        minimum = DeviceModel.xml_end(group)
        if len(payload) < minimum:
            payload += b"\x00" * (minimum - len(payload))
        group.data = bytearray(payload)
        if group.kind == "YK" and payload:
            group.result = payload[0]
        updated += 1

    for group_key, points in step.set_points.items():
        try:
            group_no, entry_no = DeviceModel.parse_group_key(str(group_key))
        except ValueError as exc:
            notes.append(f"{group_key}: {exc}")
            continue
        group = device.find(group_no, entry_no)
        if group is None:
            notes.append(f"{group_key}: 当前 XML 没有这个组")
            continue
        if not isinstance(points, dict):
            notes.append(f"{group_key}: set 必须是 {{点名: 值}}")
            continue

        for name, value in points.items():
            matched = [point for point in group.points if point.name == name]
            if not matched:
                notes.append(f"{group_key}: 没有点名 {name}")
                continue

            for point in matched:
                if point.kind == "YX":
                    if not (0 <= point.inf < 8):
                        continue
                    if point.fun >= len(group.data):
                        group.data.extend(
                            b"\x00" * (point.fun + 1 - len(group.data))
                        )
                    mask = 1 << point.inf
                    if bool(value):
                        group.data[point.fun] = (
                            group.data[point.fun] | mask
                        ) & 0xFF
                    else:
                        group.data[point.fun] = group.data[point.fun] & (
                            ~mask & 0xFF
                        )
                    updated += 1
                    continue

                if point.kind not in ("YC", "FIX") or point.inf <= 0:
                    continue
                fmt = formats.get(point_key(point), PointFormat())
                try:
                    data = fmt.encode_eng(
                        float(value), point.inf, point.factor
                    )
                except (ValueError, OverflowError, struct.error) as exc:
                    notes.append(f"{group_key}/{name}: {exc}")
                    continue
                end = point.fun + point.inf
                if end > len(group.data):
                    group.data.extend(b"\x00" * (end - len(group.data)))
                group.data[point.fun:end] = data
                updated += 1

    return updated, notes
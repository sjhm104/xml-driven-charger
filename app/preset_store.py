"""测试预设：把当前所有组的数据区存成 JSON，再一键恢复。"""

from __future__ import annotations

import json
from pathlib import Path

from app.device_model import DeviceModel, format_hex, parse_hex


def collect_preset(device: DeviceModel) -> dict:
    return {
        "xml": str(device.xml_path or ""),
        "values": {
            f"0x{group.fun_type:04X}": format_hex(bytes(group.data))
            for group in device.sorted_groups()
        },
    }


def apply_preset(device: DeviceModel, preset: dict) -> tuple[int, int, list[str]]:
    values = preset.get("values") if isinstance(preset, dict) else None
    if not isinstance(values, dict):
        return 0, 0, ["预设文件里没有 values 字典"]

    applied = 0
    skipped = 0
    notes: list[str] = []

    for key, hex_text in values.items():
        try:
            group_no, entry_no = DeviceModel.parse_group_key(str(key))
            data = parse_hex(str(hex_text))
        except (ValueError, TypeError) as exc:
            skipped += 1
            notes.append(f"{key}: {exc}")
            continue

        group = device.find(group_no, entry_no)
        if group is None:
            skipped += 1
            notes.append(f"{key}: 当前 XML 没有这个组")
            continue

        minimum = DeviceModel.xml_end(group)
        if len(data) < minimum:
            data += b"\x00" * (minimum - len(data))
        group.data = bytearray(data)
        if group.kind == "YK" and data:
            group.result = data[0]
        applied += 1

    return applied, skipped, notes


def save_preset(path: str | Path, device: DeviceModel) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(collect_preset(device), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_preset(path: str | Path) -> dict:
    path = Path(path)
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("预设文件不是 JSON 对象")
    return obj
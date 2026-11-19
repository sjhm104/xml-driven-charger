"""组模板：给没有真实报文的组提供初始数据区。

格式（data/templates.json）：
    {
      "0x1B01": "00 18 12 5C 00 28 00 64"
    }

键支持 "0x1B01"、"1B01"、"27/1"；值是一串十六进制字节。
模板比 XML 点表长出来的部分，就是 UI 里的“扩展字段”，例如 0x1B01 的
第 4 个值（电池剩余容量）。
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_TEMPLATES: dict[str, str] = {
    # 24AH, 47.00V(原始 4700), 剩余报警容量 40%, 剩余容量 100%
    "0x1B01": "00 18 12 5C 00 28 00 64",
}


def load_templates(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(DEFAULT_TEMPLATES, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        return dict(DEFAULT_TEMPLATES)

    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_TEMPLATES)

    if not isinstance(obj, dict):
        return dict(DEFAULT_TEMPLATES)

    result: dict[str, str] = {}
    for key, value in obj.items():
        key = str(key)
        if key.startswith("_"):
            continue
        result[key] = str(value)
    return result


def save_templates(path: str | Path, templates: dict[str, str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {key: templates[key] for key in sorted(templates)}
    path.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def template_key_for_frame(frame) -> str | None:
    """从响应帧推导模板键。

    0x81 -> 0x01xx（查询）
    0x82 -> 0x02xx（控制）
    0x1A/0x1B -> 0x1Bxx（定值数据组）
    0x83 -> 0x03xx（均浮充，通常没有对应点表）
    """
    func = frame.func
    subtype = frame.subtype
    if func == 0x81:
        group_no = 0x01
    elif func == 0x82:
        group_no = 0x02
    elif func in (0x1A, 0x1B):
        group_no = 0x1B
    elif func == 0x83:
        group_no = 0x03
    else:
        return None
    return f"0x{group_no:02X}{subtype:02X}"
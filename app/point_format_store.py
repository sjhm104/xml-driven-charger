"""点格式 sidecar：data/point_formats.json。

按 XML 文件名分组，避免不同型号的同名点位互相覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.point_format import PointFormat


def point_key(point) -> str:
    return f"{point.group_no}/{point.entry_no}/{point.fun}/{point.inf}/{point.kind}"


def load_formats(path: str | Path, xml_stem: str) -> dict[str, PointFormat]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(obj, dict):
        return {}

    section = obj.get(xml_stem, {})
    if not isinstance(section, dict):
        return {}

    formats: dict[str, PointFormat] = {}
    for key, value in section.items():
        fmt = PointFormat.from_dict(value)
        if fmt.code != "auto" or fmt.offset != 0 or fmt.scale is not None:
            formats[str(key)] = fmt
    return formats


def save_formats(
    path: str | Path,
    xml_stem: str,
    formats: dict[str, PointFormat],
) -> None:
    path = Path(path)

    existing: dict = {}
    if path.exists():
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                existing = obj
        except (OSError, json.JSONDecodeError):
            existing = {}

    section: dict[str, dict] = {}
    for key, fmt in formats.items():
        if fmt.code == "auto" and fmt.offset == 0 and fmt.scale is None:
            continue
        section[key] = fmt.to_dict()

    if section:
        existing[xml_stem] = section
    else:
        existing.pop(xml_stem, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
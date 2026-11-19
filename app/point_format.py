"""点级数据格式：有符号/字节序/浮点/偏移/系数。

XML 里只有 Factor，没有符号、偏移、浮点信息。这里用 sidecar 配置补齐，
不改装置类型表。
"""

from __future__ import annotations

from dataclasses import dataclass
import struct

CODE_LABELS: dict[str, str] = {
    "auto": "自动（按长度无符号大端）",
    "u8": "u8",
    "i8": "i8",
    "u16be": "u16 大端",
    "i16be": "i16 大端",
    "u16le": "u16 小端",
    "i16le": "i16 小端",
    "u32be": "u32 大端",
    "i32be": "i32 大端",
    "u32le": "u32 小端",
    "i32le": "i32 小端",
    "f32be": "float32 大端",
    "f32le": "float32 小端",
    "raw": "原始大端整数",
}

ALL_CODES = list(CODE_LABELS)

# code -> (struct format, size)
_STRUCT: dict[str, tuple[str, int]] = {
    "u8": ("B", 1),
    "i8": ("b", 1),
    "u16be": (">H", 2),
    "i16be": (">h", 2),
    "u16le": ("<H", 2),
    "i16le": ("<h", 2),
    "u32be": (">I", 4),
    "i32be": (">i", 4),
    "u32le": ("<I", 4),
    "i32le": ("<i", 4),
    "f32be": (">f", 4),
    "f32le": ("<f", 4),
}

_FLOAT_CODES = {"f32be", "f32le"}


@dataclass
class PointFormat:
    code: str = "auto"
    offset: float = 0.0
    scale: float | None = None

    def resolved_code(self, length: int) -> str:
        if self.code != "auto":
            return self.code
        return {1: "u8", 2: "u16be", 4: "u32be"}.get(length, "raw")

    def expected_length(self, length: int) -> int:
        code = self.resolved_code(length)
        if code in ("auto", "raw"):
            return length
        return _STRUCT.get(code, ("", length))[1]

    def is_float(self, length: int) -> bool:
        return self.resolved_code(length) in _FLOAT_CODES

    def effective_scale(self, default_scale: float) -> float:
        return self.scale if self.scale is not None else default_scale

    def decode_raw(self, data: bytes) -> float:
        if not data:
            return 0.0
        code = self.resolved_code(len(data))
        if code == "raw":
            return float(int.from_bytes(data, "big"))
        info = _STRUCT.get(code)
        if info is None or info[1] != len(data):
            return float(int.from_bytes(data, "big"))
        return float(struct.unpack(info[0], data)[0])

    def decode_eng(self, data: bytes, default_scale: float) -> float:
        return self.decode_raw(data) * self.effective_scale(default_scale) + self.offset

    def encode_raw(self, value: float, length: int) -> bytes:
        if length <= 0:
            return b""
        code = self.resolved_code(length)
        if code == "raw":
            return int(round(value)).to_bytes(length, "big")
        info = _STRUCT.get(code)
        if info is None or info[1] != length or code == "auto":
            return int(round(value)).to_bytes(length, "big")
        struct_format, _size = info
        if struct_format.endswith("f"):
            return struct.pack(struct_format, float(value))
        return struct.pack(struct_format, int(round(value)))

    def encode_eng(self, value: float, length: int, default_scale: float) -> bytes:
        scale = self.effective_scale(default_scale)
        if scale == 0:
            raise ValueError("系数不能为 0")
        return self.encode_raw((value - self.offset) / scale, length)

    def describe(self) -> str:
        if self.code == "auto" and self.offset == 0 and self.scale is None:
            return ""
        parts = [CODE_LABELS.get(self.code, self.code)]
        if self.offset:
            parts.append(f"offset={self.offset:g}")
        if self.scale is not None:
            parts.append(f"scale={self.scale:g}")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "offset": self.offset,
            "scale": self.scale,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PointFormat":
        if not isinstance(data, dict):
            return cls()
        code = str(data.get("code", "auto"))
        if code not in ALL_CODES:
            code = "auto"
        try:
            offset = float(data.get("offset", 0.0))
        except (TypeError, ValueError):
            offset = 0.0
        scale_raw = data.get("scale")
        try:
            scale = None if scale_raw is None else float(scale_raw)
        except (TypeError, ValueError):
            scale = None
        return cls(code=code, offset=offset, scale=scale)
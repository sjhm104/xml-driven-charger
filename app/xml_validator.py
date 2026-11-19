"""装置类型表静态检查。

只做“能看出来”的问题，不替代 DTU 侧加载校验：
- 同一组数据区重叠
- 遥信同字节/同位重复
- FUN/INF 越界
- 系数为 0
- 数据区为空或过长
- 同一组混合类型
"""

from __future__ import annotations

from dataclasses import dataclass

from app.device_model import DeviceModel, Group


@dataclass
class Issue:
    level: str  # "ERROR" / "WARN"
    group: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.group}: {self.message}"


MAX_DATA_LEN = 252  # 长度字段 255 - 地址/功能码/子类型


def validate_device(device: DeviceModel) -> list[Issue]:
    issues: list[Issue] = []

    for group in device.sorted_groups():
        label = f"0x{group.fun_type:04X}"
        if group.kind == "MIX":
            issues.append(Issue("WARN", label, "同一组里混有遥测/遥信/定值点"))

        if group.kind != "YK" and len(group.data) == 0:
            issues.append(Issue("WARN", label, "数据区长度为 0"))
        if len(group.data) > MAX_DATA_LEN:
            issues.append(
                Issue(
                    "ERROR",
                    label,
                    f"数据区 {len(group.data)} 字节，超过 {MAX_DATA_LEN}",
                )
            )

        intervals: list[tuple[int, int, str]] = []
        bits: dict[tuple[int, int], str] = {}

        for point in group.points:
            name = point.name or "(未命名)"

            if point.kind in ("YC", "FIX"):
                if point.inf <= 0:
                    issues.append(Issue("WARN", label, f"{name}: INF={point.inf}"))
                if point.fun < 0 or point.fun > 255:
                    issues.append(
                        Issue("ERROR", label, f"{name}: FUN={point.fun} 越界")
                    )
                if point.fun + max(point.inf, 0) > MAX_DATA_LEN:
                    issues.append(
                        Issue(
                            "ERROR",
                            label,
                            f"{name}: FUN+INF={point.fun + point.inf} 超过 {MAX_DATA_LEN}",
                        )
                    )
                if point.factor == 0:
                    issues.append(Issue("WARN", label, f"{name}: 系数为 0"))
                if point.inf > 0:
                    intervals.append(
                        (point.fun, point.fun + point.inf, name)
                    )

            elif point.kind == "YX":
                if not 0 <= point.inf <= 7:
                    issues.append(
                        Issue(
                            "ERROR",
                            label,
                            f"{name}: 位号 INF={point.inf} 不在 0..7",
                        )
                    )
                if point.fun < 0 or point.fun > 255:
                    issues.append(
                        Issue("ERROR", label, f"{name}: FUN={point.fun} 越界")
                    )
                bit_key = (point.fun, point.inf)
                if bit_key in bits:
                    issues.append(
                        Issue(
                            "WARN",
                            label,
                            f"{name}: 与 {bits[bit_key]} 使用同一字节/位 "
                            f"({point.fun}, {point.inf})",
                        )
                    )
                else:
                    bits[bit_key] = name

        intervals.sort()
        for previous, current in zip(intervals, intervals[1:]):
            if current[0] < previous[1]:
                issues.append(
                    Issue(
                        "WARN",
                        label,
                        f"数据区重叠: {previous[2]} "
                        f"[{previous[0]}..{previous[1] - 1}] 与 {current[2]} "
                        f"[{current[0]}..{current[1] - 1}]",
                    )
                )

    return issues
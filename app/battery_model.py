"""电池模型 + 点到角色的映射。

模型只维护 SOC/电压/电流/告警等状态，不直接依赖 Qt；
apply_snapshot() 负责把状态写到 XML 点表对应的数据区。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
from pathlib import Path
import struct

from app.device_model import DeviceModel, Point
from app.point_format import PointFormat
from app.point_format_store import point_key


@dataclass
class BatteryConfig:
    capacity_ah: float = 24.0
    initial_soc: float = 100.0
    discharge_current: float = 10.0
    charge_current: float = 5.0
    activation_current: float = 10.0
    float_voltage: float = 53.5
    charge_voltage: float = 57.0
    empty_voltage: float = 42.0
    undervoltage: float = 46.0
    cutoff: float = 43.2
    capacity_fault_soc: float = 5.0
    temperature: float = 25.0


# 遥测/定值角色（只匹配 YC/FIX）
MEASURE_ROLES: list[tuple[str, tuple[str, ...]]] = [
    ("battery_voltage", ("电池电压", "后备电源电压")),
    ("capacitor_voltage", ("电容电压", "操作电压")),
    ("discharge_current", ("放电电流",)),
    ("charge_current", ("充电电流",)),
    ("soc", ("SOC", "电池电量", "电量")),
    ("max_discharge_minutes", ("最大放电时间",)),
    ("current_discharge_minutes", ("当前放电时长", "当前放电时间")),
    ("temperature", ("温度",)),
]

# 遥信角色（只匹配 YX）
SIGNAL_ROLES: list[tuple[str, tuple[str, ...]]] = [
    ("phase_a_loss", ("A 相失电", "A相失电")),
    ("phase_b_loss", ("B 相失电", "B相失电")),
    ("phase_c_loss", ("C 相失电", "C相失电")),
    ("ac_loss", ("交流失电",)),
    ("activation", ("活化状态", "寿命监测状态")),
    ("undervoltage", ("电池欠压告警",)),
    ("cutoff", ("电池欠压切除",)),
    ("insulation", ("直流绝缘",)),
    ("capacity_fault", ("电池容量故障",)),
    ("backup_running", ("后备电源运行状态",)),
]


def _match(name: str, keywords: tuple[str, ...]) -> bool:
    lower = name.lower()
    return any(keyword.lower() in lower for keyword in keywords)


def role_for_point(point: Point) -> str | None:
    if point.kind == "YX":
        for role, keywords in SIGNAL_ROLES:
            if _match(point.name, keywords):
                return role
        return None
    if point.kind in ("YC", "FIX"):
        for role, keywords in MEASURE_ROLES:
            if _match(point.name, keywords):
                return role
        return None
    return None


@dataclass
class BatteryModel:
    config: BatteryConfig = field(default_factory=BatteryConfig)
    soc: float = 100.0
    ac_present: bool = True
    phase_loss: list[bool] = field(default_factory=lambda: [False, False, False])
    activation: bool = False
    insulation_fault: bool = False
    discharge_seconds: float = 0.0
    activation_seconds: float = 0.0
    max_discharge_minutes: float = 0.0
    discharge_current: float = 0.0
    charge_current: float = 0.0
    voltage: float = 53.5

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.soc = max(0.0, min(100.0, self.config.initial_soc))
        self.ac_present = True
        self.phase_loss = [False, False, False]
        self.activation = False
        self.insulation_fault = False
        self.discharge_seconds = 0.0
        self.activation_seconds = 0.0
        self.max_discharge_minutes = 0.0
        self.discharge_current = 0.0
        self.charge_current = 0.0
        self.voltage = (
            self.config.float_voltage
            if self.soc >= 99.99
            else self.config.charge_voltage
        )

    def start_activation(self) -> None:
        self.activation = True
        self.activation_seconds = 0.0
        self.discharge_seconds = 0.0
        self.max_discharge_minutes = 0.0

    def stop_activation(self) -> None:
        self.activation = False
        self.discharge_current = 0.0

    def ac_lost(self) -> bool:
        return (not self.ac_present) or sum(self.phase_loss) >= 2

    def tick(self, dt_s: float) -> None:
        if dt_s <= 0:
            return

        discharging = self.activation or self.ac_lost()
        if discharging:
            current = (
                self.config.activation_current
                if self.activation
                else self.config.discharge_current
            )
            self.discharge_current = max(0.0, current)
            self.charge_current = 0.0
            self.soc -= (
                self.discharge_current * dt_s / (self.config.capacity_ah * 3600.0) * 100.0
            )
            self.discharge_seconds += dt_s
            self.max_discharge_minutes = max(
                self.max_discharge_minutes, self.discharge_seconds / 60.0
            )
            if self.activation:
                self.activation_seconds += dt_s
        elif self.soc < 99.999:
            self.discharge_current = 0.0
            self.charge_current = max(0.0, self.config.charge_current)
            self.soc += (
                self.charge_current * dt_s / (self.config.capacity_ah * 3600.0) * 100.0
            )
        else:
            self.discharge_current = 0.0
            self.charge_current = 0.0

        self.soc = max(0.0, min(100.0, self.soc))

        if self.ac_lost() or self.activation:
            voltage = self.config.empty_voltage + (
                self.config.float_voltage - self.config.empty_voltage
            ) * (self.soc / 100.0)
            voltage -= self.discharge_current * 0.01
            self.voltage = max(self.config.empty_voltage, voltage)
        elif self.soc < 99.999:
            self.voltage = self.config.charge_voltage
        else:
            self.voltage = self.config.float_voltage

        if self.voltage <= self.config.cutoff:
            self.activation = False
            self.discharge_current = 0.0

    def snapshot(self) -> dict:
        ac_loss = self.ac_lost()
        undervoltage = self.voltage < self.config.undervoltage
        cutoff = self.voltage < self.config.cutoff
        capacity_fault = self.soc <= self.config.capacity_fault_soc
        return {
            "battery_voltage": self.voltage,
            "capacitor_voltage": self.config.float_voltage,
            "discharge_current": self.discharge_current,
            "charge_current": self.charge_current,
            "soc": self.soc,
            "max_discharge_minutes": self.max_discharge_minutes,
            "current_discharge_minutes": self.discharge_seconds / 60.0,
            "temperature": self.config.temperature,
            "phase_a_loss": self.phase_loss[0],
            "phase_b_loss": self.phase_loss[1],
            "phase_c_loss": self.phase_loss[2],
            "ac_loss": ac_loss,
            "activation": self.activation,
            "undervoltage": undervoltage,
            "cutoff": cutoff,
            "insulation": self.insulation_fault,
            "capacity_fault": capacity_fault,
            "backup_running": ac_loss or self.activation,
        }

    def state_text(self) -> str:
        if self.voltage <= self.config.cutoff:
            return "欠压切除"
        if self.activation:
            return "活化放电"
        if self.ac_lost():
            return "后备放电"
        if self.charge_current > 0:
            return "充电"
        return "浮充"


def apply_snapshot(
    device: DeviceModel,
    snapshot: dict,
    formats: dict[str, PointFormat] | None = None,
    group_key: tuple[int, int] | None = None,
) -> tuple[int, list[str]]:
    """把模型状态写到匹配角色的点；group_key 限定只写某一组。"""
    formats = formats or {}
    updated = 0
    notes: list[str] = []

    for group in device.groups.values():
        if group_key is not None and group.key != group_key:
            continue
        for point in group.points:
            role = role_for_point(point)
            if role is None:
                continue
            if role == "temperature" and any(
                word in point.name for word in ("告警", "阈值")
            ):
                continue

            value = snapshot.get(role)
            if value is None:
                continue

            if point.kind == "YX":
                if not (0 <= point.inf < 8):
                    continue
                if point.fun >= len(group.data):
                    group.data.extend(
                        b"\x00" * (point.fun + 1 - len(group.data))
                    )
                mask = 1 << point.inf
                if bool(value):
                    group.data[point.fun] = (group.data[point.fun] | mask) & 0xFF
                else:
                    group.data[point.fun] = group.data[point.fun] & (~mask & 0xFF)
                updated += 1
                continue

            if point.kind not in ("YC", "FIX") or point.inf <= 0:
                continue

            fmt = formats.get(point_key(point), PointFormat())
            try:
                data = fmt.encode_eng(float(value), point.inf, point.factor)
            except (ValueError, OverflowError, struct.error) as exc:
                notes.append(f"{point.name}: {exc}")
                continue

            end = point.fun + point.inf
            if end > len(group.data):
                group.data.extend(b"\x00" * (end - len(group.data)))
            group.data[point.fun:end] = data
            updated += 1

    return updated, notes


def load_battery_config(path: str | Path) -> BatteryConfig:
    path = Path(path)
    if not path.exists():
        return BatteryConfig()
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return BatteryConfig()
    if not isinstance(obj, dict):
        return BatteryConfig()

    allowed = {item.name for item in fields(BatteryConfig)}
    kwargs: dict[str, float] = {}
    for key, value in obj.items():
        if key not in allowed:
            continue
        try:
            kwargs[key] = float(value)
        except (TypeError, ValueError):
            continue
    try:
        return BatteryConfig(**kwargs)
    except TypeError:
        return BatteryConfig()


def save_battery_config(path: str | Path, config: BatteryConfig) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
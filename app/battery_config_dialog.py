"""电池模型参数对话框：电池参数 + XML 点值配置。"""

from __future__ import annotations

import math
import struct

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.battery_model import BatteryConfig
from app.device_model import DeviceModel, Group, Point
from app.point_format import PointFormat
from app.point_format_store import point_key


class BatteryConfigDialog(QDialog):
    def __init__(
        self,
        parent,
        config: BatteryConfig,
        device: DeviceModel | None = None,
        formats: dict[str, PointFormat] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("电池参数与点值")
        self.resize(780, 620)
        self._device = device
        self._formats = formats or {}
        self._point_rows: list[tuple[str, Group, Point, QTableWidgetItem]] = []
        self._extra_point_values: dict[str, float] = {}
        self.result_config: BatteryConfig | None = None

        layout = QVBoxLayout(self)
        tip = QLabel(
            "参数只保存在 data/battery_config.json，不会修改装置类型表。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_battery_tab(), "电池参数")
        self.tabs.addTab(self._build_points_tab(), "点值配置")
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        self.reset_btn = QPushButton("恢复默认")
        buttons.addWidget(self.reset_btn)
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("取消")
        self.ok_btn = QPushButton("确定")
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.ok_btn)
        layout.addLayout(buttons)

        self.reset_btn.clicked.connect(self._reset)
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn.clicked.connect(self._accept)

        self._load(config)

    def _build_battery_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        form = QFormLayout()
        self.capacity_spin = self._spin(0.1, 10000.0, 1, " Ah")
        form.addRow("容量:", self.capacity_spin)

        self.soc_spin = self._spin(0.0, 100.0, 1, " %")
        form.addRow("初始 SOC:", self.soc_spin)

        self.discharge_spin = self._spin(0.0, 1000.0, 1, " A")
        form.addRow("放电电流:", self.discharge_spin)

        self.charge_current_spin = self._spin(0.0, 1000.0, 1, " A")
        form.addRow("充电电流:", self.charge_current_spin)

        self.activation_spin = self._spin(0.0, 1000.0, 1, " A")
        form.addRow("活化电流:", self.activation_spin)

        self.float_spin = self._spin(0.0, 1000.0, 2, " V")
        form.addRow("浮充电压:", self.float_spin)

        self.charge_spin = self._spin(0.0, 1000.0, 2, " V")
        form.addRow("充电电压:", self.charge_spin)

        self.empty_spin = self._spin(0.0, 1000.0, 2, " V")
        form.addRow("放空电压:", self.empty_spin)

        self.undervoltage_spin = self._spin(0.0, 1000.0, 2, " V")
        form.addRow("欠压阈值:", self.undervoltage_spin)

        self.cutoff_spin = self._spin(0.0, 1000.0, 2, " V")
        form.addRow("切除阈值:", self.cutoff_spin)

        self.capacity_fault_spin = self._spin(0.0, 100.0, 1, " %")
        form.addRow("容量故障 SOC:", self.capacity_fault_spin)

        self.temperature_spin = self._spin(-50.0, 200.0, 1, " ℃")
        form.addRow("温度:", self.temperature_spin)

        layout.addLayout(form)
        layout.addStretch(1)
        return tab

    def _build_points_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        tip = QLabel(
            "列出当前 XML 中所有可显示的遥测/定值点。"
            "“配置值”留空表示自动；填写后，DTU 查询该组时写入。"
            "定值在下次 0x1B 读取时生效。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.points_table = QTableWidget(0, 5)
        self.points_table.setHorizontalHeaderLabels(
            ["组", "名称", "类型", "当前工程值", "配置值"]
        )
        self.points_table.setAlternatingRowColors(True)
        self.points_table.verticalHeader().setVisible(False)
        self.points_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.points_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self.points_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for column, width in enumerate((85, 250, 55, 110)):
            self.points_table.setColumnWidth(column, width)
        layout.addWidget(self.points_table, 1)

        self.points_empty_label = QLabel("")
        self.points_empty_label.setStyleSheet("color: #777;")
        layout.addWidget(self.points_empty_label)

        row = QHBoxLayout()
        row.addWidget(QLabel("配置值按工程值填写，支持小数和负数。"))
        row.addStretch(1)
        self.clear_points_btn = QPushButton("清空配置值")
        row.addWidget(self.clear_points_btn)
        layout.addLayout(row)
        self.clear_points_btn.clicked.connect(self._clear_point_values)

        self._populate_points()
        return tab

    def _populate_points(self) -> None:
        self._point_rows = []
        if self._device is None or not self._device.groups:
            self.points_empty_label.setText("加载 XML 后可配置点值。")
            return

        for group in self._device.sorted_groups():
            for point in group.points:
                if point.kind not in ("YC", "FIX") or point.inf <= 0:
                    continue

                row = self.points_table.rowCount()
                self.points_table.insertRow(row)
                key = point_key(point)

                group_item = QTableWidgetItem(f"0x{group.fun_type:04X}")
                name_item = QTableWidgetItem(point.name or "(未命名)")
                kind_item = QTableWidgetItem(point.kind)
                current_item = QTableWidgetItem(
                    self._current_value_text(group, point)
                )
                for column, item in enumerate(
                    (group_item, name_item, kind_item, current_item)
                ):
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                    )
                    self.points_table.setItem(row, column, item)

                value_item = QTableWidgetItem("")
                value_item.setToolTip("留空=自动；填写后按工程值写入")
                self.points_table.setItem(row, 4, value_item)
                self._point_rows.append((key, group, point, value_item))

        if not self._point_rows:
            self.points_empty_label.setText(
                "当前 XML 没有可配置的遥测/定值点。"
            )

    def _current_value_text(self, group: Group, point: Point) -> str:
        if point.kind in ("FIX", "RAW") and not group.read_seen:
            return "--"
        raw = group.data[point.fun : point.fun + point.inf]
        if len(raw) < point.inf:
            raw += b"\x00" * (point.inf - len(raw))
        fmt = self._formats.get(point_key(point), PointFormat())
        try:
            value = fmt.decode_eng(raw, point.factor)
        except (ValueError, OverflowError, struct.error):
            return "--"
        return f"{value:g}"

    @staticmethod
    def _spin(
        minimum: float,
        maximum: float,
        decimals: int,
        suffix: str,
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        spin.setSingleStep(0.1 if decimals >= 1 else 1.0)
        return spin

    def _load(self, config: BatteryConfig) -> None:
        self.capacity_spin.setValue(config.capacity_ah)
        self.soc_spin.setValue(config.initial_soc)
        self.discharge_spin.setValue(config.discharge_current)
        self.charge_current_spin.setValue(config.charge_current)
        self.activation_spin.setValue(config.activation_current)
        self.float_spin.setValue(config.float_voltage)
        self.charge_spin.setValue(config.charge_voltage)
        self.empty_spin.setValue(config.empty_voltage)
        self.undervoltage_spin.setValue(config.undervoltage)
        self.cutoff_spin.setValue(config.cutoff)
        self.capacity_fault_spin.setValue(config.capacity_fault_soc)
        self.temperature_spin.setValue(config.temperature)

        known_keys = {row[0] for row in self._point_rows}
        self._extra_point_values = {
            key: value
            for key, value in config.point_values.items()
            if key not in known_keys
        }
        for key, _group, _point, item in self._point_rows:
            value = config.point_values.get(key)
            item.setText("" if value is None else f"{value:g}")

    def _clear_point_values(self) -> None:
        self._extra_point_values.clear()
        for _key, _group, _point, item in self._point_rows:
            item.setText("")

    def _reset(self) -> None:
        self._load(BatteryConfig())

    def _accept(self) -> None:
        empty = self.empty_spin.value()
        cutoff = self.cutoff_spin.value()
        undervoltage = self.undervoltage_spin.value()
        float_voltage = self.float_spin.value()
        charge = self.charge_spin.value()

        if not (empty < cutoff < undervoltage < float_voltage < charge):
            QMessageBox.warning(
                self,
                "参数错误",
                "需要满足：放空电压 < 切除阈值 < 欠压阈值 < 浮充电压 < 充电电压",
            )
            return

        point_values = dict(self._extra_point_values)
        for key, _group, point, item in self._point_rows:
            text = item.text().strip()
            if not text:
                continue
            try:
                value = float(text)
            except ValueError:
                QMessageBox.warning(
                    self,
                    "参数错误",
                    f"{point.name or key} 的配置值不是有效数字：{text}",
                )
                return
            if not math.isfinite(value):
                QMessageBox.warning(
                    self,
                    "参数错误",
                    f"{point.name or key} 的配置值必须是有限数字",
                )
                return
            point_values[key] = value

        self.result_config = BatteryConfig(
            capacity_ah=self.capacity_spin.value(),
            initial_soc=self.soc_spin.value(),
            discharge_current=self.discharge_spin.value(),
            charge_current=self.charge_current_spin.value(),
            activation_current=self.activation_spin.value(),
            float_voltage=float_voltage,
            charge_voltage=charge,
            empty_voltage=empty,
            undervoltage=undervoltage,
            cutoff=cutoff,
            capacity_fault_soc=self.capacity_fault_spin.value(),
            temperature=self.temperature_spin.value(),
            point_values=point_values,
        )
        super().accept()

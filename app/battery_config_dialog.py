"""电池参数：按当前装置类型表配置遥测/定值点。"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.battery_model import BatteryConfig
from app.device_model import DeviceModel, Point
from app.point_format_store import point_key


class BatteryConfigDialog(QDialog):
    def __init__(
        self,
        parent,
        config: BatteryConfig,
        device: DeviceModel | None = None,
        defaults: dict[str, float] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("电池参数")
        self.resize(620, 560)
        self._device = device
        self._defaults = defaults or {}
        self._point_rows: list[tuple[str, Point, QTableWidgetItem]] = []
        self._extra_point_values: dict[str, float] = {}
        self.result_config: BatteryConfig | None = None

        layout = QVBoxLayout(self)
        tip = QLabel(
            "配置值初始显示默认值；修改后才作为覆盖值保存。"
            "遥测在 0x01 查询时生效，定值在 0x1B 读取时生效。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("搜索:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("参数名称")
        self.search_edit.setClearButtonEnabled(True)
        search_row.addWidget(self.search_edit, 1)
        layout.addLayout(search_row)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["名称", "配置值"])
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 170)
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel("")
        self.empty_label.setStyleSheet("color: #777;")
        layout.addWidget(self.empty_label)

        buttons = QHBoxLayout()
        buttons.addWidget(
            QLabel("配置值按工程值填写，支持小数和负数。")
        )
        buttons.addStretch(1)
        self.clear_btn = QPushButton("恢复默认")
        self.cancel_btn = QPushButton("取消")
        self.ok_btn = QPushButton("确定")
        buttons.addWidget(self.clear_btn)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.ok_btn)
        layout.addLayout(buttons)

        self.search_edit.textChanged.connect(self._apply_filter)
        self.clear_btn.clicked.connect(self._clear_point_values)
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn.clicked.connect(self._accept)

        self._populate_points()
        self._load(config)

    def _populate_points(self) -> None:
        self._point_rows = []
        if self._device is None or not self._device.groups:
            self.empty_label.setText("请先加载 XML，再配置电池参数。")
            return

        for group in self._device.sorted_groups():
            for point in group.points:
                if point.kind not in ("YC", "FIX") or point.inf <= 0:
                    continue

                row = self.table.rowCount()
                self.table.insertRow(row)
                key = point_key(point)

                name_item = QTableWidgetItem(point.name or "(未命名)")
                name_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                self.table.setItem(row, 0, name_item)

                value_item = QTableWidgetItem("")
                value_item.setToolTip("留空=自动；填写后按工程值写入")
                self.table.setItem(row, 1, value_item)
                self._point_rows.append((key, point, value_item))

        if not self._point_rows:
            self.empty_label.setText(
                "当前 XML 没有可配置的遥测/定值点。"
            )

    def _apply_filter(self, text: str) -> None:
        query = text.strip().lower()
        for row, (_key, point, _item) in enumerate(self._point_rows):
            self.table.setRowHidden(
                row, bool(query) and query not in point.name.lower()
            )

    def _load(self, config: BatteryConfig) -> None:
        known_keys = {row[0] for row in self._point_rows}
        self._extra_point_values = {
            key: value
            for key, value in config.point_values.items()
            if key not in known_keys
        }
        for key, _point, item in self._point_rows:
            value = config.point_values.get(key)
            if value is not None:
                item.setText(f"{value:g}")
            else:
                default = self._defaults.get(key)
                item.setText("" if default is None else f"{default:g}")

    def _clear_point_values(self) -> None:
        self._extra_point_values.clear()
        for key, _point, item in self._point_rows:
            default = self._defaults.get(key)
            item.setText("" if default is None else f"{default:g}")

    def _accept(self) -> None:
        point_values = dict(self._extra_point_values)
        for key, point, item in self._point_rows:
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
            default = self._defaults.get(key)
            if default is not None and abs(value - default) < 1e-9:
                continue
            point_values[key] = value

        self.result_config = BatteryConfig(point_values=point_values)
        super().accept()

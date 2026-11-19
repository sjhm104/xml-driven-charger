"""单点数据格式对话框。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.device_model import Point
from app.point_format import ALL_CODES, CODE_LABELS, PointFormat


class PointFormatDialog(QDialog):
    def __init__(self, parent, point: Point, current: PointFormat) -> None:
        super().__init__(parent)
        self.setWindowTitle("点数据格式")
        self.resize(520, 260)
        self._point = point
        self._current = current
        self.result: PointFormat | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"{point.name or '(未命名)'}    "
                f"偏移 {point.fun}   长度 {point.inf}   类型 {point.kind}"
            )
        )

        form = QFormLayout()
        self.code_combo = QComboBox()
        for code in ALL_CODES:
            self.code_combo.addItem(CODE_LABELS[code], code)
        index = self.code_combo.findData(current.code)
        self.code_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow("数据类型:", self.code_combo)

        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setDecimals(4)
        self.offset_spin.setRange(-1_000_000_000.0, 1_000_000_000.0)
        self.offset_spin.setValue(current.offset)
        form.addRow("偏移:", self.offset_spin)

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setDecimals(6)
        self.scale_spin.setRange(-1_000_000_000.0, 1_000_000_000.0)
        self.scale_spin.setValue(
            current.scale if current.scale is not None else point.factor
        )
        form.addRow("系数:", self.scale_spin)
        layout.addLayout(form)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #b35c00;")
        layout.addWidget(self.warning)

        buttons = QHBoxLayout()
        self.reset_btn = QPushButton("恢复默认")
        buttons.addWidget(self.reset_btn)
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("取消")
        self.ok_btn = QPushButton("确定")
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.ok_btn)
        layout.addLayout(buttons)

        self.code_combo.currentIndexChanged.connect(self._update_warning)
        self.reset_btn.clicked.connect(self._reset)
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn.clicked.connect(self._accept)
        self._update_warning()

    def _selected_code(self) -> str:
        return str(self.code_combo.currentData())

    def _update_warning(self) -> None:
        code = self._selected_code()
        length = self._point.inf
        if length <= 0:
            self.warning.setText("INF=0，没有可解释的数据区")
            return
        expected = PointFormat(code=code).expected_length(length)
        if code not in ("auto", "raw") and expected != length:
            self.warning.setText(
                f"类型长度 {expected} 与 INF {length} 不一致，不能保存"
            )
        else:
            self.warning.setText("")

    def _reset(self) -> None:
        self.code_combo.setCurrentIndex(self.code_combo.findData("auto"))
        self.offset_spin.setValue(0.0)
        self.scale_spin.setValue(self._point.factor)
        self._update_warning()

    def _accept(self) -> None:
        code = self._selected_code()
        length = self._point.inf
        if length <= 0:
            QMessageBox.warning(self, "格式错误", "INF=0，不能设置数据格式")
            return
        expected = PointFormat(code=code).expected_length(length)
        if code not in ("auto", "raw") and expected != length:
            QMessageBox.warning(
                self,
                "格式错误",
                f"类型长度 {expected} 与 INF {length} 不一致",
            )
            return
        if self.scale_spin.value() == 0:
            QMessageBox.warning(self, "格式错误", "系数不能为 0")
            return

        scale_value = self.scale_spin.value()
        scale = (
            None
            if abs(scale_value - self._point.factor) < 1e-12
            else scale_value
        )
        self.result = PointFormat(
            code=code,
            offset=self.offset_spin.value(),
            scale=scale,
        )
        super().accept()
"""电池模型参数对话框。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.battery_model import BatteryConfig


class BatteryConfigDialog(QDialog):
    def __init__(self, parent, config: BatteryConfig) -> None:
        super().__init__(parent)
        self.setWindowTitle("电池参数")
        self.resize(460, 420)
        self._config = config
        self.result_config: BatteryConfig | None = None

        layout = QVBoxLayout(self)
        tip = QLabel(
            "参数只保存在 data/battery_config.json，不会修改装置类型表。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        form = QFormLayout()

        self.capacity_spin = self._spin(0.1, 10000.0, 1, " Ah")
        form.addRow("容量:", self.capacity_spin)

        self.soc_spin = self._spin(0.0, 100.0, 1, " %")
        form.addRow("初始 SOC:", self.soc_spin)

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
        self.float_spin.setValue(config.float_voltage)
        self.charge_spin.setValue(config.charge_voltage)
        self.empty_spin.setValue(config.empty_voltage)
        self.undervoltage_spin.setValue(config.undervoltage)
        self.cutoff_spin.setValue(config.cutoff)
        self.capacity_fault_spin.setValue(config.capacity_fault_soc)
        self.temperature_spin.setValue(config.temperature)

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

        self.result_config = BatteryConfig(
            capacity_ah=self.capacity_spin.value(),
            initial_soc=self.soc_spin.value(),
            float_voltage=float_voltage,
            charge_voltage=charge,
            empty_voltage=empty,
            undervoltage=undervoltage,
            cutoff=cutoff,
            capacity_fault_soc=self.capacity_fault_spin.value(),
            temperature=self.temperature_spin.value(),
            # 电流保持在主界面控制
            discharge_current=self._config.discharge_current,
            charge_current=self._config.charge_current,
            activation_current=self._config.activation_current,
        )
        super().accept()
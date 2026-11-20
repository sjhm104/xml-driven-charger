"""主窗口：串口、XML 点表、数据区编辑、报文监视。"""

from __future__ import annotations

import csv
import struct
from pathlib import Path

from PySide6.QtCore import Qt, QDateTime, QSettings, QIODevice, QTimer
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.battery_model import (
    BatteryModel,
    apply_snapshot,
    load_battery_config,
    role_for_point,
    save_battery_config,
)
from app.battery_config_dialog import BatteryConfigDialog
from app.device_model import (
    KIND_NAMES,
    DeviceModel,
    Group,
    Point,
    format_hex,
    parse_hex,
)
from app.fault_injector import FaultConfig, FaultInjector
from app.frame_codec import Frame, FrameCodec
from app.point_format import PointFormat
from app.point_format_dialog import PointFormatDialog
from app.point_format_store import load_formats, point_key, save_formats
from app.preset_store import apply_preset, load_preset, save_preset
from app.protocol_engine import ProtocolEngine
from app.template_dialog import TemplateDialog
from app.template_store import load_templates
from app.xml_check_dialog import XmlCheckDialog
from app.xml_validator import validate_device

CATEGORY_ORDER = ["YC", "YX", "YK", "FIX"]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("充电机模拟器 - EB90 通用规约")
        self.resize(1180, 700)

        self.app_dir = Path(__file__).resolve().parent.parent
        self.templates_path = self.app_dir / "data" / "templates.json"
        self.formats_path = self.app_dir / "data" / "point_formats.json"
        self.settings = QSettings("ChargerSim", "ChargerSim")
        self.templates = load_templates(self.templates_path)
        self._formats: dict[str, PointFormat] = {}
        self._xml_stem = ""
        self.device = DeviceModel()
        self.engine = ProtocolEngine(self.device)
        self.fault_injector = FaultInjector()
        self.recorded_frames: list[Frame] = []
        self._last_request: tuple[int, Frame] | None = None
        self._frame_records: list[dict] = []

        self.battery_config_path = self.app_dir / "data" / "battery_config.json"
        self.battery_model = BatteryModel(
            load_battery_config(self.battery_config_path)
        )
        self.battery_timer = QTimer(self)
        self.battery_timer.setInterval(1000)
        self.battery_timer.timeout.connect(self._on_battery_tick)
        self._battery_last_alarms: dict = {}
        self._battery_last_state = ""


        self.port = QSerialPort(self)
        self._want_open = False
        self._desired_port = ""
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(2000)
        self._reconnect_timer.timeout.connect(self._try_reconnect)
        self._port_scan_timer = QTimer(self)
        self._port_scan_timer.setInterval(2000)
        self._port_scan_timer.timeout.connect(self._scan_ports)
        self._rx_buffer = bytearray()
        self._last_tx = b""
        self._last_tx_ms = 0
        self._updating = False
        self._current_group: Group | None = None
        self.category = "YC"
        self._row_specs: list[tuple] = []
        self._xml_issues: list = []
        self._xml_notes: list[str] = []
        self._rx_count = 0
        self._tx_count = 0

        self._build_ui()
        self._refresh_categories()
        self._refresh_ports()
        self._load_settings()
        self._set_port_open(False)
        self._port_scan_timer.start()
        self._update_counts()
        self._log("INFO", "工具启动")

        self.port.readyRead.connect(self._on_ready_read)
        self.port.errorOccurred.connect(self._on_serial_error)

        last_xml = str(self.settings.value("xml_path", "") or "")
        if last_xml and Path(last_xml).exists():
            self._load_xml_path(last_xml)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        top = QHBoxLayout()
        top.setSpacing(6)

        top.addWidget(QLabel("串口:"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(140)
        self.port_combo.setToolTip(
            "电脑上 USB-RS485 适配器对应的 COM 口；A/B 接到 DTU 的电源模块 485 口"
        )
        top.addWidget(self.port_combo)
        self.refresh_ports_btn = QPushButton("刷新")
        top.addWidget(self.refresh_ports_btn)

        top.addWidget(QLabel("波特率:"))
        self.baud_combo = QComboBox()
        for baud in (9600, 19200, 38400, 57600, 115200):
            self.baud_combo.addItem(str(baud), baud)
        top.addWidget(self.baud_combo)

        self.open_btn = QPushButton("打开串口")
        self.open_btn.setMinimumWidth(96)
        self.open_btn.setToolTip("必须打开串口才能收到 DTU 的轮询报文")
        top.addWidget(self.open_btn)

        self.port_state_label = QLabel("串口未打开")
        self.port_state_label.setStyleSheet(
            "color: #c0392b; font-weight: bold;"
        )
        top.addWidget(self.port_state_label)

        self.reconnect_cb = QCheckBox("自动重连")
        self.reconnect_cb.setChecked(True)
        self.reconnect_cb.setToolTip("串口异常断开后每 2 秒尝试重新打开")
        self.reconnect_cb.setVisible(False)

        top.addSpacing(10)
        top.addWidget(QLabel("地址:"))
        self.addr_spin = QSpinBox()
        self.addr_spin.setRange(1, 247)
        self.addr_spin.setValue(1)
        self.addr_spin.setFixedWidth(64)
        top.addWidget(self.addr_spin)

        self.xml_name_label = QLabel("未加载 XML")
        self.xml_name_label.setFixedWidth(170)
        self.xml_name_label.setStyleSheet("color: #555;")
        top.addWidget(self.xml_name_label)

        self.xml_btn = QPushButton("加载 XML...")
        top.addWidget(self.xml_btn)

        self.auto_cb = QCheckBox("自动应答")
        self.auto_cb.setChecked(True)
        top.addWidget(self.auto_cb)

        # 暂停/重置移到菜单，顶部保持紧凑
        self.pause_cb = QCheckBox("暂停应答")
        self.pause_cb.setVisible(False)
        self.reset_btn = QPushButton("重置数据")
        self.reset_btn.setToolTip(
            "所有组重置为模板/零值；电池模型运行时请先停止模型"
        )
        self.reset_btn.setVisible(False)
        top.addStretch(1)

        # 故障注入 + 模板
        aux = QHBoxLayout()

        fault_group = QGroupBox("故障注入")
        fault_layout = QHBoxLayout(fault_group)
        self.fault_drop_all_cb = QCheckBox("不回帧")
        self.fault_delay_spin = QSpinBox()
        self.fault_delay_spin.setRange(0, 5000)
        self.fault_delay_spin.setSingleStep(50)
        self.fault_delay_spin.setSuffix(" ms")
        self.fault_delay_spin.setFixedWidth(90)
        self.fault_delay_spin.setToolTip("每次响应延迟发送；超过 300ms 会触发 DTU 超时重发")
        self.fault_bad_crc_cb = QCheckBox("错校验")
        self.fault_wrong_addr_cb = QCheckBox("错地址+1")
        self.fault_short_cb = QCheckBox("短帧")
        self.fault_short_cb.setToolTip("少发最后 1 字节（校验和），DTU 会等超时")
        self.fault_drop_every_spin = QSpinBox()
        self.fault_drop_every_spin.setRange(0, 100)
        self.fault_drop_every_spin.setFixedWidth(60)
        self.fault_drop_every_spin.setToolTip("每 N 帧丢 1 帧，0=关闭")
        self.fault_clear_btn = QPushButton("清除故障")
        fault_layout.addWidget(self.fault_drop_all_cb)
        fault_layout.addWidget(QLabel("延迟"))
        fault_layout.addWidget(self.fault_delay_spin)
        fault_layout.addWidget(self.fault_bad_crc_cb)
        fault_layout.addWidget(self.fault_wrong_addr_cb)
        fault_layout.addWidget(self.fault_short_cb)
        fault_layout.addWidget(QLabel("丢帧 每"))
        fault_layout.addWidget(self.fault_drop_every_spin)
        fault_layout.addWidget(QLabel("帧丢1"))
        fault_layout.addWidget(self.fault_clear_btn)
        fault_layout.addStretch(1)

        template_group = QGroupBox("模板")
        template_layout = QHBoxLayout(template_group)
        self.template_btn = QPushButton("模板...")
        self.record_cb = QCheckBox("录制响应")
        self.record_cb.setToolTip("取消自动应答后，勾选此项可监听并录制真实设备响应")
        self.clear_record_btn = QPushButton("清空录制")
        template_layout.addWidget(self.template_btn)
        template_layout.addWidget(self.record_cb)
        template_layout.addWidget(self.clear_record_btn)

        aux.addWidget(fault_group, 1)
        aux.addWidget(template_group)

        self.aux_widget = QWidget()
        self.aux_widget.setLayout(aux)
        self.aux_widget.setVisible(False)

        # 左侧：按数据类型查看
        self.category_list = QListWidget()
        self.category_list.setMinimumWidth(250)
        self.category_list.setMaximumHeight(150)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("数据类型"))
        left_layout.addWidget(self.category_list)
        self.xml_summary = QLabel("未加载 XML")
        self.xml_summary.setWordWrap(True)
        self.xml_summary.setStyleSheet("color: #555;")
        left_layout.addWidget(self.xml_summary)
        self.xml_warnings = QLabel("")
        self.xml_warnings.setWordWrap(True)
        self.xml_warnings.setStyleSheet("color: #b35c00;")
        left_layout.addWidget(self.xml_warnings)
        self.xml_check_btn = QPushButton("XML 检查...")
        left_layout.addWidget(self.xml_check_btn)
        left_layout.addStretch(1)

        # 右侧：点表
        self.group_title = QLabel("未选择组")
        font = self.group_title.font()
        font.setBold(True)
        self.group_title.setFont(font)

        self.group_info = QLabel("")

        info_row = QHBoxLayout()
        info_row.addWidget(self.group_info)
        info_row.addStretch(1)
        self.format_btn = QPushButton("点格式...")
        info_row.addWidget(self.format_btn)
        info_row.addWidget(QLabel("搜索:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("名称 / 偏移 / HEX")
        self.search_edit.setFixedWidth(200)
        self.search_edit.setClearButtonEnabled(True)
        info_row.addWidget(self.search_edit)

        self.table = QTableWidget(0, 9)
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
        self.table.horizontalHeader().setStretchLastSection(True)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.group_title)
        detail_layout.addLayout(info_row)
        detail_layout.addWidget(self.table, 1)

        # 底部：报文监视 / 运行日志 分开
        self.bottom_tabs = QTabWidget()

        frame_widget = QWidget()
        frame_layout = QVBoxLayout(frame_widget)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_header = QHBoxLayout()
        frame_header.addWidget(QLabel("RX / TX / ERR / ECHO"))
        frame_header.addStretch(1)
        frame_header.addWidget(QLabel("过滤:"))
        self.frame_filter_combo = QComboBox()
        self.frame_filter_combo.addItems(["全部", "RX", "TX", "ERR", "ECHO"])
        self.frame_filter_edit = QLineEdit()
        self.frame_filter_edit.setPlaceholderText("HEX")
        self.frame_filter_edit.setFixedWidth(170)
        self.frame_autoscroll_cb = QCheckBox("自动滚动")
        self.frame_autoscroll_cb.setChecked(True)
        self.export_frame_btn = QPushButton("导出...")
        self.clear_frame_btn = QPushButton("清空")
        frame_header.addWidget(self.frame_filter_combo)
        frame_header.addWidget(self.frame_filter_edit)
        frame_header.addWidget(self.frame_autoscroll_cb)
        frame_header.addWidget(self.export_frame_btn)
        frame_header.addWidget(self.clear_frame_btn)
        self.frame_view = QPlainTextEdit()
        self.frame_view.setReadOnly(True)
        self.frame_view.setFont(QFont("Consolas", 9))
        self.frame_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.frame_view.document().setMaximumBlockCount(5000)
        frame_layout.addLayout(frame_header)
        frame_layout.addWidget(self.frame_view)
        self.bottom_tabs.addTab(frame_widget, "报文监视")

        event_widget = QWidget()
        event_layout = QVBoxLayout(event_widget)
        event_layout.setContentsMargins(0, 0, 0, 0)
        event_header = QHBoxLayout()
        event_header.addWidget(QLabel("INFO / WARN / FAULT / REC / SKIP"))
        event_header.addStretch(1)
        self.event_autoscroll_cb = QCheckBox("自动滚动")
        self.event_autoscroll_cb.setChecked(True)
        self.export_event_btn = QPushButton("导出...")
        self.clear_event_btn = QPushButton("清空")
        event_header.addWidget(self.event_autoscroll_cb)
        event_header.addWidget(self.export_event_btn)
        event_header.addWidget(self.clear_event_btn)
        self.event_view = QPlainTextEdit()
        self.event_view.setReadOnly(True)
        self.event_view.setFont(QFont("Consolas", 9))
        self.event_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.event_view.document().setMaximumBlockCount(5000)
        event_layout.addLayout(event_header)
        event_layout.addWidget(self.event_view)
        self.bottom_tabs.addTab(event_widget, "运行日志")

        battery_group = QGroupBox("电池模型")
        battery_layout = QVBoxLayout(battery_group)
        battery_layout.setSpacing(4)

        row1 = QHBoxLayout()
        self.battery_start_btn = QPushButton("启动")
        self.battery_stop_btn = QPushButton("停止")
        self.battery_reset_btn = QPushButton("重置")
        row1.addWidget(self.battery_start_btn)
        row1.addWidget(self.battery_stop_btn)
        row1.addWidget(self.battery_reset_btn)
        battery_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.battery_activation_btn = QPushButton("活化启动")
        self.battery_activation_stop_btn = QPushButton("活化停止")
        self.battery_config_btn = QPushButton("参数...")
        row2.addWidget(self.battery_activation_btn)
        row2.addWidget(self.battery_activation_stop_btn)
        row2.addWidget(self.battery_config_btn)
        battery_layout.addLayout(row2)

        row3 = QHBoxLayout()
        self.battery_ac_cb = QCheckBox("交流供电")
        self.battery_ac_cb.setChecked(True)
        self.battery_phase_cbs = [
            QCheckBox(text) for text in ("A相失电", "B相失电", "C相失电")
        ]
        row3.addWidget(self.battery_ac_cb)
        for checkbox in self.battery_phase_cbs:
            row3.addWidget(checkbox)
        battery_layout.addLayout(row3)

        row4 = QHBoxLayout()
        self.battery_load_spin = QDoubleSpinBox()
        self.battery_load_spin.setDecimals(1)
        self.battery_load_spin.setRange(0.0, 1000.0)
        self.battery_load_spin.setValue(10.0)
        self.battery_load_spin.setSuffix(" A")
        self.battery_load_spin.setFixedWidth(80)
        self.battery_charge_spin = QDoubleSpinBox()
        self.battery_charge_spin.setDecimals(1)
        self.battery_charge_spin.setRange(0.0, 1000.0)
        self.battery_charge_spin.setValue(5.0)
        self.battery_charge_spin.setSuffix(" A")
        self.battery_charge_spin.setFixedWidth(80)
        row4.addWidget(QLabel("负载"))
        row4.addWidget(self.battery_load_spin)
        row4.addWidget(QLabel("充电"))
        row4.addWidget(self.battery_charge_spin)
        battery_layout.addLayout(row4)

        row5 = QHBoxLayout()
        self.battery_soc_label = QLabel("SOC --")
        self.battery_voltage_label = QLabel("-- V")
        row5.addWidget(self.battery_soc_label)
        row5.addWidget(self.battery_voltage_label)
        row5.addStretch(1)
        battery_layout.addLayout(row5)

        self.battery_state_label = QLabel("状态 --")
        battery_layout.addWidget(self.battery_state_label)

        left_layout.insertWidget(left_layout.count() - 1, battery_group)

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(detail)
        right.addWidget(self.bottom_tabs)
        right.setSizes([520, 200])

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setMinimumWidth(310)
        left_scroll.setWidget(left)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_scroll)
        splitter.addWidget(right)
        splitter.setSizes([330, 850])

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.addLayout(top)
        root_layout.addWidget(self.aux_widget)
        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(root)

        self.status_label = QLabel("串口未打开")
        self.count_label = QLabel("RX 0 / TX 0")
        self.sim_status_label = QLabel("电池模型: 停止")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.count_label)
        self.statusBar().addPermanentWidget(self.sim_status_label)

        self.refresh_ports_btn.clicked.connect(self._refresh_ports)
        self.open_btn.clicked.connect(self._toggle_port)
        self.xml_btn.clicked.connect(self._choose_xml)
        self.xml_check_btn.clicked.connect(self._check_xml)
        self.reset_btn.clicked.connect(self._reset_data)
        self.auto_cb.toggled.connect(self._update_status)
        self.pause_cb.toggled.connect(self._update_status)
        self.reconnect_cb.toggled.connect(self._on_reconnect_toggled)
        self.addr_spin.valueChanged.connect(self._update_status)
        self.category_list.currentItemChanged.connect(self._on_category_selected)
        self.table.currentCellChanged.connect(self._on_table_row_changed)
        self.table.itemChanged.connect(self._on_item_changed)
        self.search_edit.textChanged.connect(self._apply_point_filter)
        self.format_btn.clicked.connect(self._edit_point_format)
        self.clear_frame_btn.clicked.connect(self._clear_frame_log)
        self.clear_event_btn.clicked.connect(self.event_view.clear)
        self.frame_filter_combo.currentIndexChanged.connect(self._render_frame_log)
        self.frame_filter_edit.textChanged.connect(self._render_frame_log)
        self.export_frame_btn.clicked.connect(self._export_frames)
        self.export_event_btn.clicked.connect(self._export_events)
        self.fault_clear_btn.clicked.connect(self._clear_faults)
        self.template_btn.clicked.connect(self._open_templates)
        self.clear_record_btn.clicked.connect(self._clear_recorded)

        self._build_menu()

        self.battery_start_btn.clicked.connect(self._start_battery)
        self.battery_stop_btn.clicked.connect(self._stop_battery)
        self.battery_activation_btn.clicked.connect(self._start_activation)
        self.battery_activation_stop_btn.clicked.connect(self._stop_activation)
        self.battery_reset_btn.clicked.connect(self._reset_battery)
        self.battery_config_btn.clicked.connect(self._edit_battery_config)
        self.battery_ac_cb.toggled.connect(self._on_battery_controls_changed)
        for checkbox in self.battery_phase_cbs:
            checkbox.toggled.connect(self._on_battery_controls_changed)
        self.battery_load_spin.valueChanged.connect(self._on_battery_controls_changed)
        self.battery_charge_spin.valueChanged.connect(
            self._on_battery_controls_changed
        )
        self._update_battery_buttons()
        self._update_battery_labels()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        file_menu.addAction("加载 XML...", self._choose_xml)
        file_menu.addSeparator()
        file_menu.addAction("保存预设...", self._save_preset)
        file_menu.addAction("加载预设...", self._load_preset)
        file_menu.addSeparator()
        file_menu.addAction("重置数据", self._reset_data)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)

        tool_menu = self.menuBar().addMenu("工具")
        self.pause_action = tool_menu.addAction("暂停应答")
        self.pause_action.setCheckable(True)
        self.pause_action.toggled.connect(self.pause_cb.setChecked)
        self.reconnect_action = tool_menu.addAction("自动重连")
        self.reconnect_action.setCheckable(True)
        self.reconnect_action.setChecked(True)
        self.reconnect_action.toggled.connect(self.reconnect_cb.setChecked)
        tool_menu.addSeparator()
        tool_menu.addAction("点格式...", self._edit_point_format)
        tool_menu.addAction("XML 检查...", self._check_xml)
        tool_menu.addSeparator()
        self.aux_action = tool_menu.addAction("显示高级工具栏")
        self.aux_action.setCheckable(True)
        self.aux_action.toggled.connect(self.aux_widget.setVisible)

    # ------------------------------------------------------------- settings

    def _load_settings(self) -> None:
        port = str(self.settings.value("port", "") or "")
        if port:
            index = self.port_combo.findData(port)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

        try:
            baud = int(self.settings.value("baud", 9600))
        except (TypeError, ValueError):
            baud = 9600
        index = self.baud_combo.findData(baud)
        if index >= 0:
            self.baud_combo.setCurrentIndex(index)

        try:
            self.addr_spin.setValue(int(self.settings.value("address", 1)))
        except (TypeError, ValueError):
            self.addr_spin.setValue(1)

        auto = str(self.settings.value("auto_reply", "true")).lower()
        self.auto_cb.setChecked(auto in ("1", "true", "yes"))
        reconnect = str(self.settings.value("auto_reconnect", "true")).lower()
        self.reconnect_cb.setChecked(reconnect in ("1", "true", "yes"))
        if hasattr(self, "reconnect_action"):
            self.reconnect_action.setChecked(self.reconnect_cb.isChecked())

    def _save_settings(self) -> None:
        self.settings.setValue("port", self.port_combo.currentData() or "")
        self.settings.setValue("baud", self.baud_combo.currentData() or 9600)
        self.settings.setValue("address", self.addr_spin.value())
        self.settings.setValue("auto_reply", self.auto_cb.isChecked())
        self.settings.setValue("auto_reconnect", self.reconnect_cb.isChecked())
        if self.device.xml_path:
            self.settings.setValue("xml_path", str(self.device.xml_path))
        self.settings.sync()

    # --------------------------------------------------------------- serial

    def _refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        for info in QSerialPortInfo.availablePorts():
            label = f"{info.portName()} - {info.description()}"
            self.port_combo.addItem(label, info.portName())
        if self.port_combo.count() == 0:
            self.port_combo.addItem("无可用串口", "")
        elif current:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def _toggle_port(self) -> None:
        if self.port.isOpen():
            self._want_open = False
            self._reconnect_timer.stop()
            self.port.close()
            self._set_port_open(False)
            self._log("INFO", "串口已关闭")
            return
        self._open_port(silent=False)

    def _open_port(self, silent: bool = False) -> bool:
        name = self.port_combo.currentData()
        if not name:
            if not silent:
                QMessageBox.warning(self, "提示", "没有可用串口")
            return False

        if self.port.isOpen():
            self.port.close()

        self.port.setPortName(str(name))
        self.port.setBaudRate(int(self.baud_combo.currentData()))
        self.port.setDataBits(QSerialPort.DataBits.Data8)
        self.port.setParity(QSerialPort.Parity.NoParity)
        self.port.setStopBits(QSerialPort.StopBits.OneStop)
        self.port.setFlowControl(QSerialPort.FlowControl.NoFlowControl)

        if self.port.open(QIODevice.OpenModeFlag.ReadWrite):
            self._want_open = True
            self._desired_port = str(name)
            self._reconnect_timer.stop()
            self._set_port_open(True)
            self._log(
                "INFO",
                f"串口已打开 {name} @ {self.baud_combo.currentData()} 8N1",
            )
            return True

        self._log("ERR", "串口打开失败")
        if not silent:
            QMessageBox.critical(self, "串口打开失败", "串口打开失败")
        return False

    def _try_reconnect(self) -> None:
        if self.port.isOpen():
            self._reconnect_timer.stop()
            return
        if not self._want_open or not self.reconnect_cb.isChecked():
            self._reconnect_timer.stop()
            return
        if self._open_port(silent=True):
            self._log("INFO", "自动重连成功")
        else:
            self._log("WARN", "自动重连失败，继续重试")

    def _on_reconnect_toggled(self, checked: bool) -> None:
        if not checked:
            self._reconnect_timer.stop()

    def _scan_ports(self) -> None:
        if self.port.isOpen():
            return
        names = [info.portName() for info in QSerialPortInfo.availablePorts()]

        # 自动重连等待期间，不要让热插拔把选择切到别的 COM 口
        if self._want_open and self._desired_port and self._desired_port not in names:
            return

        existing = [
            self.port_combo.itemData(index)
            for index in range(self.port_combo.count())
            if self.port_combo.itemData(index)
        ]
        if names == existing:
            return

        current = self.port_combo.currentData()
        self._refresh_ports()
        if current:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def _set_port_open(self, opened: bool) -> None:
        self.open_btn.setText("关闭串口" if opened else "打开串口")
        self.port_state_label.setText("串口已打开" if opened else "串口未打开")
        self.port_state_label.setStyleSheet(
            "color: #1e8449; font-weight: bold;"
            if opened
            else "color: #c0392b; font-weight: bold;"
        )
        self.port_combo.setEnabled(not opened)
        self.baud_combo.setEnabled(not opened)
        self.refresh_ports_btn.setEnabled(not opened)
        self._update_status()

    def _on_serial_error(self, error) -> None:
        if error == QSerialPort.SerialPortError.NoError:
            return
        self._log("ERR", f"串口错误: {self.port.errorString()}")
        if error in (
            QSerialPort.SerialPortError.ResourceError,
            QSerialPort.SerialPortError.PermissionError,
        ):
            if self.port.isOpen():
                self.port.close()
            self._set_port_open(False)
            if self._want_open and self.reconnect_cb.isChecked():
                self._reconnect_timer.start()
                self._log("INFO", "串口异常，2 秒后自动重连")
            else:
                self._want_open = False

    def _on_ready_read(self) -> None:
        data = bytes(self.port.readAll())
        if not data:
            return
        self._rx_buffer.extend(data)
        for frame in FrameCodec.extract(self._rx_buffer):
            self._handle_frame(frame)

    # --------------------------------------------------------------- frames

    def _handle_frame(self, frame: Frame) -> None:
        if not frame.valid:
            self._log_frame(
                "ERR",
                f"{frame.error} | {frame.raw.hex(' ').upper()}",
                frame=frame,
            )
            return

        self._rx_count += 1
        self._update_counts()
        self._log_frame("RX", frame.hex(), frame=frame)

        now = QDateTime.currentMSecsSinceEpoch()
        if frame.raw == self._last_tx and (now - self._last_tx_ms) < 300:
            self._log_frame("ECHO", "忽略本机发送的回声", frame=frame)
            return

        if frame.addr != self.addr_spin.value():
            self._log("SKIP", f"地址不匹配: {frame.addr} != {self.addr_spin.value()}")
            return

        request_funcs = (0x01, 0x02, 0x1A, 0x1B, 0x83, 0x03)
        previous_request = self._last_request

        if self._is_recordable_response(frame, now, previous_request):
            self.recorded_frames.append(frame)
            if len(self.recorded_frames) > 100:
                self.recorded_frames.pop(0)
            self._log("REC", frame.summary())
            return

        if frame.func in request_funcs:
            self._last_request = (now, frame)
        else:
            return

        if not self.auto_cb.isChecked() or self.pause_cb.isChecked():
            self._log("SKIP", "未应答（自动应答关闭或暂停）")
            return

        if frame.func == 0x1A and frame.subtype == 0x0C:
            self.device.reset_defaults(self.templates)
            self._defer_fix_values()
            self._populate_category(self.category)

        if frame.func == 0x02:
            control_group = self.device.find(0x02, frame.subtype)
            if control_group is not None:
                control_name = (
                    control_group.points[0].name
                    if control_group.points
                    else ""
                )
                for group in self.device.groups.values():
                    if group.kind == "YK":
                        group.closed = group is control_group
                if self.category == "YK":
                    self._refresh_current_values()
                self._log("INFO", f"收到遥控: {control_name or '未命名'}")

        if self.battery_timer.isActive():
            if frame.func == 0x02:
                control_group = self.device.find(0x02, frame.subtype)
                control_name = (
                    control_group.points[0].name
                    if control_group and control_group.points
                    else ""
                )
                if "活化启动" in control_name:
                    self.battery_model.start_activation()
                    self._log("MODEL", "DTU 遥控: 活化启动")
                elif "活化停止" in control_name:
                    self.battery_model.stop_activation()
                    self._log("MODEL", "DTU 遥控: 活化停止")
                elif "放电" in control_name and "停止" in control_name:
                    self.battery_model.stop_activation()
                    self._log("MODEL", "DTU 遥控: 停止放电")
            elif frame.func == 0x1A and frame.subtype == 0x0C:
                self.battery_model.reset()
                self._log("MODEL", "DTU 遥控: 恢复出厂")

        if self.battery_timer.isActive() and frame.func == 0x01:
            group = self.device.find(0x01, frame.subtype)
            if group is not None:
                self._apply_battery_snapshot(group.key)
                self._refresh_current_values()

        if frame.func == 0x1B:
            group = self.device.find(0x1B, frame.subtype)
            if group is not None and not group.read_seen:
                self._apply_template_to_group(group)
                group.read_seen = True
                if self.category == "FIX":
                    self._populate_category("FIX")
        elif frame.func == 0x1A and frame.subtype != 0x0C:
            group = self.device.find(0x1B, frame.subtype)
            if group is not None and not group.read_seen:
                self._apply_template_to_group(group)
                group.read_seen = True

        response = self.engine.handle(frame)
        if response is None:
            self._log("SKIP", self.engine.last_note or "未定义请求，未应答")
            return

        if self.engine.last_note:
            self._log("INFO", self.engine.last_note)

        fault = self.fault_injector.apply(
            response, self._fault_config(), self.addr_spin.value()
        )
        if fault.notes:
            self._log("FAULT", "; ".join(fault.notes))
        if fault.data is None:
            return

        if fault.delay_ms > 0:
            self._log("INFO", f"{fault.delay_ms} ms 后发送响应")
            QTimer.singleShot(
                fault.delay_ms,
                lambda data=fault.data: self._send_response(data),
            )
        else:
            self._send_response(fault.data)

        if (
            frame.func == 0x1A
            and frame.subtype != 0x0C
            and self.category == "FIX"
        ):
            self._populate_category("FIX")

    def _send_response(self, data: bytes) -> None:
        if not self.port.isOpen():
            self._log("ERR", "串口未打开，取消发送")
            return
        if len(data) < 6:
            self._log("ERR", "响应长度异常，取消发送")
            return

        written = self.port.write(data)
        if written < 0:
            self._log("ERR", f"发送失败: {self.port.errorString()}")
            return
        self.port.flush()

        self._tx_count += 1
        self._update_counts()
        tx_frame = Frame(data, data[3], data[4], data[5], data[6:-1])
        self._log_frame("TX", tx_frame.hex(), frame=tx_frame)
        self._last_tx = data
        self._last_tx_ms = QDateTime.currentMSecsSinceEpoch()

    def _is_recordable_response(
        self,
        frame: Frame,
        now: int,
        previous_request: tuple[int, Frame] | None,
    ) -> bool:
        if not self.record_cb.isChecked():
            return False
        if frame.addr != self.addr_spin.value():
            return False

        # 0x81/0x82 只可能是响应
        if frame.func in (0x81, 0x82):
            return True

        # 0x1A/0x1B/0x83 请求和响应功能码相同，用“请求后 20~280ms、
        # 同地址/功能码/子类型、有数据区”判断响应。
        if frame.func in (0x1A, 0x1B, 0x83):
            if not frame.data or previous_request is None:
                return False
            timestamp, request = previous_request
            delta = now - timestamp
            return (
                20 <= delta <= 280
                and request.addr == frame.addr
                and request.func == frame.func
                and request.subtype == frame.subtype
            )
        return False

    def _fault_config(self) -> FaultConfig:
        return FaultConfig(
            drop_all=self.fault_drop_all_cb.isChecked(),
            delay_ms=self.fault_delay_spin.value(),
            bad_checksum=self.fault_bad_crc_cb.isChecked(),
            wrong_address=self.fault_wrong_addr_cb.isChecked(),
            short_frame=self.fault_short_cb.isChecked(),
            drop_every_n=self.fault_drop_every_spin.value(),
        )

    def _clear_faults(self) -> None:
        self.fault_drop_all_cb.setChecked(False)
        self.fault_delay_spin.setValue(0)
        self.fault_bad_crc_cb.setChecked(False)
        self.fault_wrong_addr_cb.setChecked(False)
        self.fault_short_cb.setChecked(False)
        self.fault_drop_every_spin.setValue(0)
        self.fault_injector.reset()
        self._log("INFO", "故障注入已清除")

    def _clear_recorded(self) -> None:
        self.recorded_frames.clear()
        self._log("INFO", "录制缓存已清空")

    def _open_templates(self) -> None:
        row = self.table.currentRow()
        selected_group = (
            self._row_specs[row][1]
            if 0 <= row < len(self._row_specs)
            else None
        )
        dialog = TemplateDialog(
            self,
            self.templates,
            selected_group,
            self.recorded_frames,
            self.templates_path,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        self.templates = dict(dialog.templates)
        if dialog.apply_now:
            self.device.reset_defaults(self.templates)
            self._defer_fix_values()
            self._refresh_categories()
            self._log("INFO", "模板已保存并应用（所有组已按模板重置）")
        else:
            self._log("INFO", "模板已保存（下次加载 XML 时生效）")

    def _defer_fix_values(self) -> None:
        """定值组在收到 0x1B 读请求前不显示值。"""
        for group in self.device.groups.values():
            if group.kind in ("FIX", "RAW"):
                group.read_seen = False
                group.data = bytearray(len(group.data))

    def _apply_template_to_group(self, group: Group) -> None:
        for key, hex_text in self.templates.items():
            try:
                group_no, entry_no = DeviceModel.parse_group_key(key)
            except ValueError:
                continue
            if (group_no, entry_no) != group.key:
                continue
            try:
                data = parse_hex(hex_text)
            except ValueError:
                return
            size = max(len(group.data), len(data))
            new_data = bytearray(size)
            new_data[: len(data)] = data
            group.data = new_data
            return

    def _mark_fix_read(self) -> None:
        for group in self.device.groups.values():
            if group.kind in ("FIX", "RAW") and any(group.data):
                group.read_seen = True

    # -------------------------------------------------------------- battery

    def _start_battery(self) -> None:
        if not self.device.groups:
            QMessageBox.information(self, "电池模型", "请先加载 XML")
            return
        self.battery_model.reset()
        self._apply_battery_ui_to_model()
        self._battery_last_alarms = {}
        self._battery_last_state = ""
        self._clear_model_values()
        self._update_battery_labels()
        self.battery_timer.start()
        self._update_battery_buttons()
        self._log(
            "MODEL",
            "电池模型已启动；等待 DTU 轮询后更新点表",
        )

    def _stop_battery(self) -> None:
        if not self.battery_timer.isActive():
            return
        self.battery_timer.stop()
        self._update_battery_buttons()
        self._log("MODEL", "电池模型已停止")

    def _reset_battery(self) -> None:
        self.battery_model.reset()
        self._apply_battery_ui_to_model()
        self._clear_model_values()
        self._update_battery_labels()
        self._battery_last_alarms = {}
        self._battery_last_state = ""
        self._log("MODEL", "电池模型已重置；点表等待轮询")

    def _edit_battery_config(self) -> None:
        dialog = BatteryConfigDialog(self, self.battery_model.config)
        if (
            dialog.exec() != QDialog.DialogCode.Accepted
            or dialog.result_config is None
        ):
            return

        running = self.battery_timer.isActive()
        self.battery_model.config = dialog.result_config
        try:
            save_battery_config(self.battery_config_path, dialog.result_config)
        except OSError as exc:
            self._log("ERR", f"保存电池参数失败: {exc}")

        self.battery_model.reset()
        self._apply_battery_ui_to_model()
        self._clear_model_values()
        self._update_battery_labels()
        self._battery_last_alarms = {}
        self._battery_last_state = ""
        self._log(
            "MODEL",
            "电池参数已更新；点表等待轮询"
            + ("，模型已重置" if running else ""),
        )

    def _start_activation(self) -> None:
        self.battery_model.start_activation()
        self._log("MODEL", "活化启动")
        self._update_battery_labels()

    def _stop_activation(self) -> None:
        self.battery_model.stop_activation()
        self._log("MODEL", "活化停止")
        self._update_battery_labels()

    def _apply_battery_ui_to_model(self) -> None:
        self.battery_model.config.discharge_current = self.battery_load_spin.value()
        self.battery_model.config.activation_current = self.battery_load_spin.value()
        self.battery_model.config.charge_current = self.battery_charge_spin.value()
        self.battery_model.ac_present = self.battery_ac_cb.isChecked()
        self.battery_model.phase_loss = [
            checkbox.isChecked() for checkbox in self.battery_phase_cbs
        ]

    def _on_battery_controls_changed(self, *_args) -> None:
        self._apply_battery_ui_to_model()
        if self.battery_timer.isActive():
            self._update_battery_labels()

    def _on_battery_tick(self) -> None:
        self.battery_model.tick(1.0)
        self._update_battery_labels()
        self._log_battery_transitions()

    def _log_battery_transitions(self) -> None:
        snapshot = self.battery_model.snapshot()
        labels = {
            "ac_loss": "交流失电",
            "undervoltage": "电池欠压",
            "cutoff": "电池欠压切除",
            "activation": "活化",
            "backup_running": "后备投入",
            "capacity_fault": "电池容量故障",
        }
        for key, label in labels.items():
            value = bool(snapshot[key])
            old = self._battery_last_alarms.get(key)
            if old is not None and old != value:
                self._log("MODEL", f"{label}: {'ON' if value else 'OFF'}")
            self._battery_last_alarms[key] = value

        state = self.battery_model.state_text()
        if state != self._battery_last_state:
            self._log("MODEL", f"状态: {state}")
            self._battery_last_state = state

    def _apply_battery_snapshot(
        self, group_key: tuple[int, int] | None = None
    ) -> int:
        snapshot = self.battery_model.snapshot()
        updated, notes = apply_snapshot(
            self.device, snapshot, self._formats, group_key
        )
        for note in notes:
            self._log("WARN", note)
        return updated

    def _clear_model_values(self) -> None:
        for group in self.device.groups.values():
            for point in group.points:
                role = role_for_point(point)
                if role is None:
                    continue
                if role == "temperature" and any(
                    word in point.name for word in ("告警", "阈值")
                ):
                    continue
                if point.kind == "YX":
                    if (
                        0 <= point.fun < len(group.data)
                        and 0 <= point.inf < 8
                    ):
                        group.data[point.fun] &= ~(1 << point.inf) & 0xFF
                elif point.kind in ("YC", "FIX") and point.inf > 0:
                    end = point.fun + point.inf
                    if end > len(group.data):
                        group.data.extend(b"\x00" * (end - len(group.data)))
                    group.data[point.fun:end] = b"\x00" * point.inf
        self._refresh_current_values()

    def _update_battery_labels(self) -> None:
        self.battery_soc_label.setText(f"SOC {self.battery_model.soc:.1f}%")
        self.battery_voltage_label.setText(
            f"电压 {self.battery_model.voltage:.2f} V"
        )
        self.battery_state_label.setText(
            f"状态: {self.battery_model.state_text()}"
        )

    def _update_battery_buttons(self) -> None:
        running = self.battery_timer.isActive()
        self.battery_start_btn.setEnabled(not running)
        self.battery_stop_btn.setEnabled(running)
        self.battery_activation_btn.setEnabled(running)
        self.battery_activation_stop_btn.setEnabled(running)
        self._update_sim_status()

    def _refresh_current_values(self) -> None:
        if not self._row_specs:
            return
        if self.table.state() == QAbstractItemView.State.EditingState:
            return

        self._updating = True
        self.table.blockSignals(True)
        try:
            for row, spec in enumerate(self._row_specs):
                if row >= self.table.rowCount():
                    break
                group = spec[1]
                if group.kind in ("FIX", "RAW") and not group.read_seen:
                    for column in (4, 5, 6, 7):
                        cell = self.table.item(row, column)
                        if cell is not None:
                            cell.setText("--")
                    continue
                if spec[0] == "ext":
                    start, length = spec[2]
                    raw = self._read_range(group, start, length)
                    show_decimal = 0 < length <= 4
                    raw_dec = str(raw) if show_decimal else "-"
                    self.table.item(row, 4).setText(
                        format_hex(group.data[start : start + length])
                    )
                    self.table.item(row, 5).setText(raw_dec)
                    self.table.item(row, 7).setText(raw_dec)
                    continue

                point = spec[2]
                if point.kind == "YX":
                    byte_value = (
                        group.data[point.fun] if point.fun < len(group.data) else 0
                    )
                    checked = (
                        bool((byte_value >> point.inf) & 0x01)
                        if 0 <= point.inf < 8
                        else False
                    )
                    item = self.table.item(row, 4)
                    if item is not None:
                        self._style_yx_item(item, checked)
                    continue

                if point.kind == "YK":
                    item = self.table.item(row, 4)
                    if item is not None:
                        self._style_yx_item(item, group.closed)
                    continue

                if point.kind not in ("YC", "FIX"):
                    continue
                fmt = self._format_for(point)
                raw_bytes = self._read_point_bytes(group, point)
                raw_value = fmt.decode_raw(raw_bytes)
                scale = fmt.effective_scale(point.factor)
                engineering = fmt.decode_eng(raw_bytes, point.factor)
                show_decimal = 0 < point.inf <= 4
                if fmt.is_float(point.inf) and point.inf == 4:
                    raw_dec = f"{raw_value:g}"
                elif show_decimal:
                    raw_dec = str(int(round(raw_value)))
                else:
                    raw_dec = "-"
                self.table.item(row, 4).setText(format_hex(raw_bytes))
                self.table.item(row, 5).setText(raw_dec)
                self.table.item(row, 6).setText(f"{scale:g}")
                self.table.item(row, 7).setText(
                    f"{engineering:g}" if show_decimal else "-"
                )
        finally:
            self.table.blockSignals(False)
            self._updating = False

    def _update_sim_status(self) -> None:
        if not hasattr(self, "sim_status_label"):
            return
        battery = "运行" if self.battery_timer.isActive() else "停止"
        self.sim_status_label.setText(f"电池模型: {battery}")

    # ------------------------------------------------------------------ xml

    def _choose_xml(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择装置类型表",
            str(self.app_dir),
            "XML 文件 (*.xml);;所有文件 (*)",
        )
        if path:
            self._load_xml_path(path)

    def _set_xml_name(self, name: str, path: str = "") -> None:
        metrics = self.xml_name_label.fontMetrics()
        self.xml_name_label.setText(
            metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, 165)
        )
        self.xml_name_label.setToolTip(path or name)

    def _load_xml_path(self, path: str) -> None:
        if self.battery_timer.isActive():
            self.battery_timer.stop()
            self._update_battery_buttons()
            self._log("INFO", "加载 XML，电池模型已停止")

        try:
            self.device.load_xml(path)
            self.templates = load_templates(self.templates_path)
            self.device.apply_templates(self.templates)
            self._defer_fix_values()
            self._xml_stem = Path(path).stem
            self._formats = load_formats(self.formats_path, self._xml_stem)
        except Exception as exc:  # noqa: BLE001 - 显示给用户即可
            self.device.clear()
            self._formats = {}
            self._xml_stem = ""
            self._refresh_categories()
            self._xml_issues = []
            self._xml_notes = []
            self._set_xml_name("未加载 XML")
            self.xml_summary.setText("XML 加载失败")
            self.xml_summary.setToolTip(path)
            self.xml_warnings.setText("⚠ XML 加载失败")
            self._log("ERR", f"XML 加载失败: {exc}")
            QMessageBox.critical(self, "XML 加载失败", f"{path}\n\n{exc}")
            return

        self._refresh_categories()
        counts = self.device.point_counts()
        summary = (
            f"遥测 {counts['YC']} / 遥信 {counts['YX']} / "
            f"遥控 {counts['YK']} / 定值 {counts['FIX']}"
        )
        self._set_xml_name(Path(path).stem, path)
        self.xml_summary.setText(summary)
        self.xml_summary.setToolTip(path)
        self._log(
            "INFO",
            f"已加载 XML: {path} | {summary} | 模板 {len(self.templates)}",
        )
        warnings: list[str] = []
        if not self.device.groups:
            warnings.append("XML 没有任何有效组")
        else:
            if counts["YC"] + counts["YX"] == 0:
                warnings.append("没有遥测/遥信点（MeasureDefine/SignalDefine 为空）")
            if counts["YK"] == 0:
                warnings.append("没有遥控点（ControlDefine 为空）")
            if counts["FIX"] == 0:
                warnings.append("没有定值点（FixValDefine/SetPointDefine 为空）")
        for warning in warnings:
            self._log("WARN", warning)

        issues = validate_device(self.device)
        self._xml_notes = list(warnings)
        self._xml_issues = issues
        for issue in issues:
            tag = "ERR" if issue.level == "ERROR" else "WARN"
            self._log(tag, str(issue))

        total = len(warnings) + len(issues)
        if total:
            first = warnings[0] if warnings else issues[0].message
            suffix = f"（共 {total} 项）" if total > 1 else ""
            self.xml_warnings.setText(f"⚠ {first}{suffix}")
        else:
            self.xml_warnings.setText("")

    def _check_xml(self) -> None:
        if self.device.xml_path is None:
            QMessageBox.information(self, "XML 检查", "请先加载 XML")
            return
        dialog = XmlCheckDialog(
            self,
            str(self.device.xml_path),
            self._xml_notes,
            self._xml_issues,
        )
        dialog.exec()

    def _save_preset(self) -> None:
        if not self.device.groups:
            QMessageBox.information(self, "保存预设", "请先加载 XML")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存预设",
            str(self.app_dir / "data" / "preset.json"),
            "JSON (*.json)",
        )
        if not path:
            return
        try:
            save_preset(path, self.device)
        except OSError as exc:
            QMessageBox.critical(self, "保存预设失败", str(exc))
            return
        self._log("INFO", f"预设已保存: {path}")

    def _load_preset(self) -> None:
        if not self.device.groups:
            QMessageBox.information(self, "加载预设", "请先加载 XML")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "加载预设",
            str(self.app_dir / "data"),
            "JSON (*.json);;所有文件 (*)",
        )
        if not path:
            return
        try:
            preset = load_preset(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "加载预设失败", str(exc))
            return

        preset_xml = str(preset.get("xml", "") or "")
        if preset_xml and self.device.xml_path is not None:
            if Path(preset_xml).name != self.device.xml_path.name:
                QMessageBox.information(
                    self,
                    "预设提示",
                    f"预设来自 {Path(preset_xml).name}，只应用当前 XML 中存在的组。",
                )

        applied, skipped, notes = apply_preset(self.device, preset)
        self._mark_fix_read()
        self._populate_category(self.category)
        self._log("INFO", f"预设已加载: 应用 {applied} 组，跳过 {skipped} 组")
        for note in notes:
            self._log("WARN", note)
        if applied == 0:
            QMessageBox.warning(self, "加载预设", "没有匹配到任何组")

    def _refresh_categories(self) -> None:
        counts = self.device.point_counts()
        current = self.category if self.category in CATEGORY_ORDER else "YC"
        self.category_list.blockSignals(True)
        self.category_list.clear()
        for kind in CATEGORY_ORDER:
            item = QListWidgetItem(f"{KIND_NAMES[kind]}  ({counts[kind]})")
            item.setData(Qt.ItemDataRole.UserRole, kind)
            self.category_list.addItem(item)
        self.category_list.blockSignals(False)
        self.category_list.setCurrentRow(CATEGORY_ORDER.index(current))

    def _on_category_selected(
        self, item: QListWidgetItem | None, _previous=None
    ) -> None:
        if item is None:
            return
        kind = item.data(Qt.ItemDataRole.UserRole)
        if kind not in CATEGORY_ORDER:
            return
        self.category = kind
        self._current_group = None
        self._populate_category(kind)

    def _on_table_row_changed(
        self,
        current_row: int,
        _current_column: int,
        _previous_row: int,
        _previous_column: int,
    ) -> None:
        if 0 <= current_row < len(self._row_specs):
            self._current_group = self._row_specs[current_row][1]
        else:
            self._current_group = None

    def _collect_category_rows(self, kind: str) -> list[tuple]:
        rows: list[tuple] = []
        for group in self.device.sorted_groups():
            for point in group.points:
                if point.kind == kind:
                    rows.append(("point", group, point))
        return rows

    def _style_table(self, headers: list[str]) -> None:
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        if len(headers) == 9:
            widths = [65, 150, 50, 45, 110, 80, 55, 80]
        elif len(headers) == 6:
            widths = [65, 170, 55, 45, 90]
        else:
            widths = []
        for index, width in enumerate(widths):
            self.table.setColumnWidth(index, width)

    @staticmethod
    def _make_item(text: str, editable: bool = False) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _apply_point_filter(self) -> None:
        if not hasattr(self, "search_edit"):
            return
        text = self.search_edit.text().strip().lower()
        for row in range(self.table.rowCount()):
            if not text:
                self.table.setRowHidden(row, False)
                continue
            matched = False
            for column in range(self.table.columnCount()):
                item = self.table.item(row, column)
                if item is not None and text in item.text().lower():
                    matched = True
                    break
            self.table.setRowHidden(row, not matched)

    def _clear_table_widgets(self) -> None:
        for row in range(self.table.rowCount()):
            for column in range(self.table.columnCount()):
                if self.table.cellWidget(row, column) is not None:
                    self.table.removeCellWidget(row, column)

    def _clear_detail(self) -> None:
        self.group_title.setText("未选择数据类型")
        self.group_info.setText("")
        self.table.blockSignals(True)
        self._clear_table_widgets()
        self.table.clear()
        self.table.clearSpans()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.table.blockSignals(False)
        self._row_specs = []

    def _populate_category(self, kind: str) -> None:
        self._row_specs = []
        self.table.blockSignals(True)
        self._clear_table_widgets()
        self.table.clear()
        self.table.clearSpans()
        self.table.setRowCount(0)

        rows = self._collect_category_rows(kind)
        self.group_title.setText(KIND_NAMES.get(kind, kind))
        self.group_info.setText(f"共 {len(rows)} 行")

        if kind == "YK":
            headers = ["组", "名称", "功能码", "子类型", "值", "说明"]
            self._style_table(headers)
            self.table.setRowCount(max(1, len(rows)))
            if not rows:
                self.table.setItem(0, 0, self._make_item("没有遥控点"))
                self.table.setSpan(0, 0, 1, len(headers))
            for row, spec in enumerate(rows):
                self._row_specs.append(spec)
                group = spec[1]
                point = spec[2]
                self.table.setItem(
                    row, 0, self._make_item(f"0x{group.fun_type:04X}")
                )
                self.table.setItem(row, 1, self._make_item(point.name))
                self.table.setItem(
                    row, 2, self._make_item(f"0x{point.group_no:02X}")
                )
                self.table.setItem(
                    row, 3, self._make_item(f"0x{point.entry_no:02X}")
                )
                item = self._make_item("")
                self._style_yx_item(item, group.closed)
                self.table.setItem(row, 4, item)
                self.table.setItem(
                    row,
                    5,
                    self._make_item(
                        f"ID={point.ied_dot}" if point.ied_dot else ""
                    ),
                )
        elif kind == "YX":
            headers = ["组", "名称", "字节", "位", "值", "说明"]
            self._style_table(headers)
            self.table.setRowCount(max(1, len(rows)))
            if not rows:
                self.table.setItem(0, 0, self._make_item("没有遥信点"))
                self.table.setSpan(0, 0, 1, len(headers))
            for row, spec in enumerate(rows):
                self._row_specs.append(spec)
                group = spec[1]
                point = spec[2]
                byte_value = (
                    group.data[point.fun] if point.fun < len(group.data) else 0
                )
                checked = (
                    bool((byte_value >> point.inf) & 0x01)
                    if 0 <= point.inf < 8
                    else False
                )
                self.table.setItem(
                    row, 0, self._make_item(f"0x{group.fun_type:04X}")
                )
                self.table.setItem(row, 1, self._make_item(point.name))
                self.table.setItem(row, 2, self._make_item(str(point.fun)))
                self.table.setItem(row, 3, self._make_item(str(point.inf)))
                item = self._make_item("")
                self._style_yx_item(item, checked)
                self.table.setItem(row, 4, item)
                self.table.setItem(
                    row,
                    5,
                    self._make_item(
                        f"ID={point.ied_dot}" if point.ied_dot else ""
                    ),
                )
        else:
            headers = [
                "组",
                "名称",
                "偏移",
                "长度",
                "原始值(HEX)",
                "原始值",
                "系数",
                "工程值",
                "说明",
            ]
            self._style_table(headers)
            self.table.setRowCount(max(1, len(rows)))
            if not rows:
                self.table.setItem(0, 0, self._make_item("没有数据点"))
                self.table.setSpan(0, 0, 1, len(headers))
            for row, spec in enumerate(rows):
                self._row_specs.append(spec)
                if spec[0] == "ext":
                    group = spec[1]
                    start, length = spec[2]
                    self._populate_extension_row(group, row, start, length)
                else:
                    group = spec[1]
                    point = spec[2]
                    self._populate_numeric_point_row(group, row, point)

        self.table.blockSignals(False)
        self._apply_point_filter()

    def _populate_numeric_point_row(
        self, group: Group, row: int, point: Point
    ) -> None:
        if group.kind == "FIX" and not group.read_seen:
            self.table.setItem(
                row, 0, self._make_item(f"0x{group.fun_type:04X}")
            )
            self.table.setItem(row, 1, self._make_item(point.name or "(未命名)"))
            self.table.setItem(row, 2, self._make_item(str(point.fun)))
            self.table.setItem(row, 3, self._make_item(str(point.inf)))
            for column in (4, 5, 6, 7):
                self.table.setItem(row, column, self._make_item("--"))
            note_parts = []
            if point.ied_dot:
                note_parts.append(f"ID={point.ied_dot}")
            if point.kind == "FIX" and point.value_type:
                note_parts.append(f"ValueType={point.value_type}")
            self.table.setItem(
                row, 8, self._make_item(" | ".join(note_parts))
            )
            return

        fmt = self._format_for(point)
        raw_bytes = self._read_point_bytes(group, point)
        raw_hex = format_hex(raw_bytes)
        raw_value = fmt.decode_raw(raw_bytes)
        scale = fmt.effective_scale(point.factor)
        engineering_value = fmt.decode_eng(raw_bytes, point.factor)

        show_decimal = 0 < point.inf <= 4
        if fmt.is_float(point.inf) and point.inf == 4:
            raw_dec = f"{raw_value:g}"
        elif show_decimal:
            raw_dec = str(int(round(raw_value)))
        else:
            raw_dec = "-"
        engineering = f"{engineering_value:g}" if show_decimal else "-"

        note_parts = []
        description = fmt.describe()
        if description:
            note_parts.append(description)
        if point.ied_dot:
            note_parts.append(f"ID={point.ied_dot}")
        if point.kind == "FIX" and point.value_type:
            note_parts.append(f"ValueType={point.value_type}")

        coefficient_editable = (
            point.kind == "FIX"
            and fmt.code == "auto"
            and fmt.scale is None
        )

        self.table.setItem(
            row, 0, self._make_item(f"0x{group.fun_type:04X}")
        )
        self.table.setItem(row, 1, self._make_item(point.name or "(未命名)"))
        self.table.setItem(row, 2, self._make_item(str(point.fun)))
        self.table.setItem(row, 3, self._make_item(str(point.inf)))
        self.table.setItem(
            row, 4, self._make_item(raw_hex, editable=point.inf > 0)
        )
        self.table.setItem(row, 5, self._make_item(raw_dec, editable=show_decimal))
        self.table.setItem(
            row,
            6,
            self._make_item(f"{scale:g}", editable=coefficient_editable),
        )
        self.table.setItem(
            row, 7, self._make_item(engineering, editable=show_decimal)
        )
        self.table.setItem(row, 8, self._make_item(" | ".join(note_parts)))

    def _populate_extension_row(
        self, group: Group, row: int, start: int, length: int
    ) -> None:
        name = "原始数据" if not group.points else "扩展字段"
        if group.kind in ("FIX", "RAW") and not group.read_seen:
            self.table.setItem(
                row, 0, self._make_item(f"0x{group.fun_type:04X}")
            )
            self.table.setItem(
                row,
                1,
                self._make_item(f"{name} [{start}..{start + length - 1}]"),
            )
            self.table.setItem(row, 2, self._make_item(str(start)))
            self.table.setItem(row, 3, self._make_item(str(length)))
            for column in (4, 5, 6, 7):
                self.table.setItem(row, column, self._make_item("--"))
            self.table.setItem(
                row, 8, self._make_item("未读取（等待 0x1B 请求）")
            )
            return

        raw = self._read_range(group, start, length)
        raw_hex = format_hex(group.data[start : start + length])
        show_decimal = 0 < length <= 4
        raw_dec = str(raw) if show_decimal else "-"
        self.table.setItem(
            row, 0, self._make_item(f"0x{group.fun_type:04X}")
        )
        self.table.setItem(
            row,
            1,
            self._make_item(f"{name} [{start}..{start + length - 1}]"),
        )
        self.table.setItem(row, 2, self._make_item(str(start)))
        self.table.setItem(row, 3, self._make_item(str(length)))
        self.table.setItem(row, 4, self._make_item(raw_hex, editable=True))
        self.table.setItem(
            row, 5, self._make_item(raw_dec, editable=show_decimal)
        )
        self.table.setItem(row, 6, self._make_item("1"))
        self.table.setItem(row, 7, self._make_item(raw_dec))
        self.table.setItem(row, 8, self._make_item("未映射字节，直接改原始值"))

    # ----------------------------------------------------------- value edits

    def _format_for(self, point: Point) -> PointFormat:
        return self._formats.get(point_key(point), PointFormat())

    def _read_point_bytes(self, group: Group, point: Point) -> bytes:
        if point.inf <= 0:
            return b""
        end = point.fun + point.inf
        if end > len(group.data):
            group.data.extend(b"\x00" * (end - len(group.data)))
        return bytes(group.data[point.fun:end])

    def _write_point_bytes(self, group: Group, point: Point, data: bytes) -> bool:
        if point.inf <= 0 or len(data) != point.inf:
            return False
        end = point.fun + point.inf
        if end > len(group.data):
            group.data.extend(b"\x00" * (end - len(group.data)))
        group.data[point.fun:end] = data
        return True

    def _edit_point_format(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._row_specs):
            QMessageBox.information(self, "点格式", "请先在点表里选择一个点")
            return
        spec = self._row_specs[row]
        if spec[0] != "point" or spec[2].kind not in ("YC", "FIX"):
            QMessageBox.information(self, "点格式", "只有遥测/定值点可以设置数据格式")
            return

        point = spec[2]
        dialog = PointFormatDialog(self, point, self._format_for(point))
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result is None:
            return

        self._formats[point_key(point)] = dialog.result
        try:
            save_formats(self.formats_path, self._xml_stem, self._formats)
        except OSError as exc:
            self._log("ERR", f"保存点格式失败: {exc}")

        self._populate_category(self.category)
        self._log(
            "INFO",
            f"点格式已更新: {point.name or '(未命名)'} -> "
            f"{dialog.result.describe() or 'auto'}",
        )

    def _style_yx_item(self, item: QTableWidgetItem, closed: bool) -> None:
        item.setText("合" if closed else "分")
        item.setForeground(
            QBrush(QColor("#c0392b" if closed else "#2c3e50"))
        )
        font = item.font()
        font.setBold(closed)
        item.setFont(font)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def _refresh_numeric_row(self, group: Group, point: Point, row: int) -> None:
        self._updating = True
        self.table.blockSignals(True)
        try:
            fmt = self._format_for(point)
            raw_bytes = self._read_point_bytes(group, point)
            raw_value = fmt.decode_raw(raw_bytes)
            scale = fmt.effective_scale(point.factor)
            engineering_value = fmt.decode_eng(raw_bytes, point.factor)

            show_decimal = 0 < point.inf <= 4
            if fmt.is_float(point.inf) and point.inf == 4:
                raw_dec = f"{raw_value:g}"
            elif show_decimal:
                raw_dec = str(int(round(raw_value)))
            else:
                raw_dec = "-"

            self.table.item(row, 4).setText(format_hex(raw_bytes))
            self.table.item(row, 5).setText(raw_dec)
            self.table.item(row, 6).setText(f"{scale:g}")
            self.table.item(row, 7).setText(
                f"{engineering_value:g}" if 0 < point.inf <= 4 else "-"
            )
        finally:
            self.table.blockSignals(False)
            self._updating = False

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return

        row = item.row()
        column = item.column()
        if row < 0 or row >= len(self._row_specs):
            return
        spec = self._row_specs[row]
        group = spec[1]
        if group.kind in ("FIX", "RAW") and not group.read_seen:
            return

        if spec[0] == "point" and spec[2].kind == "YK":
            return

        if spec[0] == "ext":
            start, length = spec[2]
            if column == 4:
                try:
                    data = parse_hex(item.text())
                except ValueError:
                    self._log("ERR", f"HEX 格式错误: {item.text()}")
                    self._refresh_extension_row(group, row, start, length)
                    return
                if len(data) != length:
                    self._log("ERR", f"扩展字段长度应为 {length} 字节")
                    self._refresh_extension_row(group, row, start, length)
                    return
                raw = int.from_bytes(data, "big") if data else 0
                if not self._write_range(group, start, length, raw):
                    self._refresh_extension_row(group, row, start, length)
                    return
            elif column == 5:
                try:
                    raw = int(item.text().strip(), 0)
                except ValueError:
                    self._log("ERR", f"原始值必须是整数: {item.text()}")
                    self._refresh_extension_row(group, row, start, length)
                    return
                if not self._write_range(group, start, length, raw):
                    self._refresh_extension_row(group, row, start, length)
                    return
            else:
                return
            self._refresh_extension_row(group, row, start, length)
            return

        point = spec[2]
        if point.kind == "YX":
            return

        fmt = self._format_for(point)

        if column == 4:
            try:
                data = parse_hex(item.text())
            except ValueError:
                self._log("ERR", f"HEX 格式错误: {item.text()}")
                self._refresh_numeric_row(group, point, row)
                return
            if len(data) != point.inf:
                self._log("ERR", f"HEX 长度应为 {point.inf} 字节")
                self._refresh_numeric_row(group, point, row)
                return
            if not self._write_point_bytes(group, point, data):
                self._refresh_numeric_row(group, point, row)
                return
        elif column == 5:
            try:
                text = item.text().strip()
                value = float(text) if fmt.is_float(point.inf) else int(text, 0)
                data = fmt.encode_raw(value, point.inf)
            except (ValueError, OverflowError, struct.error):
                self._log("ERR", f"原始值格式错误: {item.text()}")
                self._refresh_numeric_row(group, point, row)
                return
            if not self._write_point_bytes(group, point, data):
                self._refresh_numeric_row(group, point, row)
                return
        elif column == 6:
            if not (
                point.kind == "FIX"
                and fmt.code == "auto"
                and fmt.scale is None
            ):
                return
            try:
                value = float(item.text().strip())
            except ValueError:
                self._log("ERR", f"系数必须是数字: {item.text()}")
                self._refresh_numeric_row(group, point, row)
                return
            if value == 0:
                self._log("ERR", "系数不能为 0")
                self._refresh_numeric_row(group, point, row)
                return
            point.factor = value
            self._refresh_numeric_row(group, point, row)
            return
        elif column == 7:
            try:
                value = float(item.text().strip())
                data = fmt.encode_eng(value, point.inf, point.factor)
            except (ValueError, OverflowError, struct.error) as exc:
                self._log("ERR", f"工程值换算失败: {exc}")
                self._refresh_numeric_row(group, point, row)
                return
            if not self._write_point_bytes(group, point, data):
                self._refresh_numeric_row(group, point, row)
                return
        else:
            return

        self._refresh_numeric_row(group, point, row)

    @staticmethod
    def _unmapped_ranges(group: Group) -> list[tuple[int, int]]:
        """找出数据区里没有被 XML 点覆盖的连续区间。"""
        total = len(group.data)
        if total <= 0:
            return []

        intervals: list[tuple[int, int]] = []
        for point in group.points:
            if point.kind in ("YC", "FIX") and point.inf > 0:
                start = max(0, min(point.fun, total))
                end = max(start, min(point.fun + point.inf, total))
                if end > start:
                    intervals.append((start, end))
            elif point.kind == "YX" and 0 <= point.fun < total:
                intervals.append((point.fun, point.fun + 1))

        if not intervals:
            return [(0, total)]

        intervals.sort()
        merged: list[list[int]] = []
        for start, end in intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)

        gaps: list[tuple[int, int]] = []
        cursor = 0
        for start, end in merged:
            if cursor < start:
                gaps.append((cursor, start - cursor))
            cursor = max(cursor, end)
        if cursor < total:
            gaps.append((cursor, total - cursor))
        return gaps

    def _refresh_extension_row(
        self, group: Group, row: int, start: int, length: int
    ) -> None:
        self._updating = True
        self.table.blockSignals(True)
        try:
            raw = self._read_range(group, start, length)
            show_decimal = 0 < length <= 4
            raw_dec = str(raw) if show_decimal else "-"
            self.table.item(row, 4).setText(
                format_hex(group.data[start : start + length])
            )
            self.table.item(row, 5).setText(raw_dec)
            self.table.item(row, 7).setText(raw_dec)
        finally:
            self.table.blockSignals(False)
            self._updating = False

    def _read_range(self, group: Group, start: int, length: int) -> int:
        if length <= 0:
            return 0
        end = start + length
        if end > len(group.data):
            group.data.extend(b"\x00" * (end - len(group.data)))
        return int.from_bytes(group.data[start:end], "big")

    def _write_range(
        self, group: Group, start: int, length: int, raw: int
    ) -> bool:
        if length <= 0:
            return False
        max_raw = (1 << (8 * length)) - 1
        if raw < 0 or raw > max_raw:
            self._log("ERR", f"扩展字段超出范围 0..{max_raw}")
            return False
        end = start + length
        if end > len(group.data):
            group.data.extend(b"\x00" * (end - len(group.data)))
        group.data[start:end] = int(raw).to_bytes(length, "big")
        return True

    def _reset_data(self) -> None:
        if not self.device.groups:
            return
        self.device.reset_defaults(self.templates)
        self._defer_fix_values()
        self._populate_category(self.category)
        self._log("INFO", "所有组已重置为模板/零值；定值等待 0x1B 读取")

    # ------------------------------------------------------------------ misc

    def _update_status(self) -> None:
        if not hasattr(self, "status_label"):
            return
        state = "已打开" if self.port.isOpen() else "未打开"
        auto = "开" if self.auto_cb.isChecked() else "关"
        paused = " | 暂停" if self.pause_cb.isChecked() else ""
        self.status_label.setText(
            f"串口{state} | 地址 {self.addr_spin.value()} | 自动应答 {auto}{paused}"
        )

    def _update_counts(self) -> None:
        self.count_label.setText(f"RX {self._rx_count} / TX {self._tx_count}")

    def _log(self, tag: str, text: str) -> None:
        timestamp = QDateTime.currentDateTime().toString("HH:mm:ss.zzz")
        line = f"[{timestamp}] {tag:<5} {text}"
        self._append_line(self.event_view, self.event_autoscroll_cb, line)

    def _log_frame(self, tag: str, text: str, frame: Frame | None = None) -> None:
        timestamp = QDateTime.currentDateTime().toString("HH:mm:ss.zzz")
        line = f"[{timestamp}] {tag:<5} {text}"
        record = {
            "time": timestamp,
            "tag": tag,
            "text": text,
            "line": line,
            "frame": frame,
        }
        self._frame_records.append(record)
        if len(self._frame_records) > 5000:
            self._frame_records.pop(0)
        if self._frame_filter_matches(record):
            self._append_line(self.frame_view, self.frame_autoscroll_cb, line)

    def _frame_filter_matches(self, record: dict) -> bool:
        direction = self.frame_filter_combo.currentText()
        if direction and direction != "全部" and record["tag"] != direction:
            return False
        query = self.frame_filter_edit.text().strip().lower()
        if query and query not in record["text"].lower():
            return False
        return True

    def _render_frame_log(self, *_args) -> None:
        self.frame_view.clear()
        for record in self._frame_records:
            if self._frame_filter_matches(record):
                self._append_line(
                    self.frame_view,
                    self.frame_autoscroll_cb,
                    record["line"],
                )

    def _clear_frame_log(self) -> None:
        self._frame_records.clear()
        self.frame_view.clear()

    def _export_frames(self) -> None:
        records = [
            record
            for record in self._frame_records
            if self._frame_filter_matches(record)
        ]
        if not records:
            QMessageBox.information(self, "导出报文", "当前过滤条件下没有报文")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出报文",
            str(self.app_dir / "data" / "frames.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        try:
            self._write_frames_csv(path, records)
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self._log("INFO", f"已导出 {len(records)} 条报文: {path}")

    @staticmethod
    def _write_frames_csv(path: str, records: list[dict]) -> None:
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["时间", "方向", "原始帧"])
            for record in records:
                frame = record.get("frame")
                frame_text = frame.hex() if frame is not None else record["text"]
                writer.writerow([record["time"], record["tag"], frame_text])

    def _export_events(self) -> None:
        text = self.event_view.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "导出运行日志", "运行日志为空")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出运行日志",
            str(self.app_dir / "data" / "events.txt"),
            "文本 (*.txt)",
        )
        if not path:
            return
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self._log("INFO", f"运行日志已导出: {path}")

    @staticmethod
    def _append_line(
        view: QPlainTextEdit,
        autoscroll: QCheckBox,
        line: str,
    ) -> None:
        view.appendPlainText(line)
        if autoscroll.isChecked():
            bar = view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def closeEvent(self, event) -> None:
        self._save_settings()
        self._port_scan_timer.stop()
        self._reconnect_timer.stop()
        self.battery_timer.stop()
        if self.port.isOpen():
            self.port.close()
        super().closeEvent(event)
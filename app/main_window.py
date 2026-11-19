"""主窗口：串口、XML 点表、数据区编辑、报文监视。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QDateTime, QSettings, QIODevice, QTimer
from PySide6.QtGui import QFont
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

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
from app.protocol_engine import ProtocolEngine
from app.template_dialog import TemplateDialog
from app.template_store import load_templates


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("充电机模拟器 - EB90 通用规约")
        self.resize(1280, 820)

        self.app_dir = Path(__file__).resolve().parent.parent
        self.templates_path = self.app_dir / "data" / "templates.json"
        self.settings = QSettings("ChargerSim", "ChargerSim")
        self.templates = load_templates(self.templates_path)
        self.device = DeviceModel()
        self.engine = ProtocolEngine(self.device)
        self.fault_injector = FaultInjector()
        self.recorded_frames: list[Frame] = []
        self._last_request: tuple[int, Frame] | None = None

        self.port = QSerialPort(self)
        self._rx_buffer = bytearray()
        self._last_tx = b""
        self._last_tx_ms = 0
        self._updating = False
        self._current_group: Group | None = None
        self._row_specs: list[tuple] = []
        self._rx_count = 0
        self._tx_count = 0

        self._build_ui()
        self._refresh_ports()
        self._load_settings()
        self._set_port_open(False)
        self._update_counts()
        self._log("INFO", "工具启动。请选择串口并加载装置类型表 XML。")

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
        self.port_combo.setMinimumWidth(230)
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
        top.addWidget(self.open_btn)

        top.addSpacing(10)
        top.addWidget(QLabel("地址:"))
        self.addr_spin = QSpinBox()
        self.addr_spin.setRange(1, 247)
        self.addr_spin.setValue(1)
        self.addr_spin.setFixedWidth(64)
        top.addWidget(self.addr_spin)

        self.xml_btn = QPushButton("加载 XML...")
        top.addWidget(self.xml_btn)

        self.auto_cb = QCheckBox("自动应答")
        self.auto_cb.setChecked(True)
        top.addWidget(self.auto_cb)

        self.pause_cb = QCheckBox("暂停应答")
        top.addWidget(self.pause_cb)

        self.reset_btn = QPushButton("重置数据")
        top.addWidget(self.reset_btn)
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

        # 左侧组列表
        self.group_list = QListWidget()
        self.group_list.setMinimumWidth(250)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("组（功能码 / 子类型）"))
        self.xml_summary = QLabel("未加载 XML")
        self.xml_summary.setWordWrap(True)
        self.xml_summary.setStyleSheet("color: #555;")
        left_layout.addWidget(self.xml_summary)
        self.xml_warnings = QLabel("")
        self.xml_warnings.setWordWrap(True)
        self.xml_warnings.setStyleSheet("color: #b35c00;")
        left_layout.addWidget(self.xml_warnings)
        left_layout.addWidget(self.group_list)

        # 右侧：点表
        self.group_title = QLabel("未选择组")
        font = self.group_title.font()
        font.setBold(True)
        self.group_title.setFont(font)

        self.group_info = QLabel("")

        info_row = QHBoxLayout()
        info_row.addWidget(self.group_info)
        info_row.addStretch(1)

        self.table = QTableWidget(0, 8)
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
        self.frame_autoscroll_cb = QCheckBox("自动滚动")
        self.frame_autoscroll_cb.setChecked(True)
        self.clear_frame_btn = QPushButton("清空")
        frame_header.addWidget(self.frame_autoscroll_cb)
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
        self.clear_event_btn = QPushButton("清空")
        event_header.addWidget(self.event_autoscroll_cb)
        event_header.addWidget(self.clear_event_btn)
        self.event_view = QPlainTextEdit()
        self.event_view.setReadOnly(True)
        self.event_view.setFont(QFont("Consolas", 9))
        self.event_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.event_view.document().setMaximumBlockCount(5000)
        event_layout.addLayout(event_header)
        event_layout.addWidget(self.event_view)
        self.bottom_tabs.addTab(event_widget, "运行日志")

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(detail)
        right.addWidget(self.bottom_tabs)
        right.setSizes([560, 220])

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([270, 990])

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.addLayout(top)
        root_layout.addLayout(aux)
        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(root)

        self.status_label = QLabel("串口未打开")
        self.count_label = QLabel("RX 0 / TX 0")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.count_label)

        self.refresh_ports_btn.clicked.connect(self._refresh_ports)
        self.open_btn.clicked.connect(self._toggle_port)
        self.xml_btn.clicked.connect(self._choose_xml)
        self.reset_btn.clicked.connect(self._reset_data)
        self.auto_cb.toggled.connect(self._update_status)
        self.pause_cb.toggled.connect(self._update_status)
        self.addr_spin.valueChanged.connect(self._update_status)
        self.group_list.currentItemChanged.connect(self._on_group_selected)
        self.table.itemChanged.connect(self._on_item_changed)
        self.clear_frame_btn.clicked.connect(self.frame_view.clear)
        self.clear_event_btn.clicked.connect(self.event_view.clear)
        self.fault_clear_btn.clicked.connect(self._clear_faults)
        self.template_btn.clicked.connect(self._open_templates)
        self.clear_record_btn.clicked.connect(self._clear_recorded)

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

    def _save_settings(self) -> None:
        self.settings.setValue("port", self.port_combo.currentData() or "")
        self.settings.setValue("baud", self.baud_combo.currentData() or 9600)
        self.settings.setValue("address", self.addr_spin.value())
        self.settings.setValue("auto_reply", self.auto_cb.isChecked())
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
            self.port.close()
            self._set_port_open(False)
            self._log("INFO", "串口已关闭")
            return

        name = self.port_combo.currentData()
        if not name:
            QMessageBox.warning(self, "提示", "没有可用串口")
            return

        self.port.setPortName(str(name))
        self.port.setBaudRate(int(self.baud_combo.currentData()))
        self.port.setDataBits(QSerialPort.DataBits.Data8)
        self.port.setParity(QSerialPort.Parity.NoParity)
        self.port.setStopBits(QSerialPort.StopBits.OneStop)
        self.port.setFlowControl(QSerialPort.FlowControl.NoFlowControl)

        if self.port.open(QIODevice.OpenModeFlag.ReadWrite):
            self._set_port_open(True)
            self._log(
                "INFO",
                f"串口已打开 {name} @ {self.baud_combo.currentData()} 8N1",
            )
        else:
            self._log("ERR", f"打开 {name} 失败: {self.port.errorString()}")
            QMessageBox.critical(self, "串口错误", self.port.errorString())

    def _set_port_open(self, opened: bool) -> None:
        self.open_btn.setText("关闭串口" if opened else "打开串口")
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
            self._log_frame("ERR", f"{frame.error} | {frame.raw.hex(' ').upper()}")
            return

        self._rx_count += 1
        self._update_counts()
        self._log_frame("RX", frame.summary())

        now = QDateTime.currentMSecsSinceEpoch()
        if frame.raw == self._last_tx and (now - self._last_tx_ms) < 300:
            self._log_frame("ECHO", "忽略本机发送的回声")
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
            if self._current_group is not None:
                self._populate_group(self._current_group)

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
            self._current_group is not None
            and frame.func == 0x1A
            and frame.subtype != 0x0C
            and self._current_group.key == (0x1B, frame.subtype)
        ):
            self._populate_group(self._current_group)

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
        self._log_frame("TX", tx_frame.summary())
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
        dialog = TemplateDialog(
            self,
            self.templates,
            self._current_group,
            self.recorded_frames,
            self.templates_path,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        self.templates = dict(dialog.templates)
        if dialog.apply_now:
            self.device.reset_defaults(self.templates)
            self._refresh_group_list()
            self._log("INFO", "模板已保存并应用（所有组已按模板重置）")
        else:
            self._log("INFO", "模板已保存（下次加载 XML 时生效）")

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

    def _load_xml_path(self, path: str) -> None:
        try:
            self.device.load_xml(path)
            self.templates = load_templates(self.templates_path)
            self.device.apply_templates(self.templates)
        except Exception as exc:  # noqa: BLE001 - 显示给用户即可
            self.device.clear()
            self._refresh_group_list()
            self.xml_summary.setText("XML 加载失败")
            self.xml_summary.setToolTip(path)
            self.xml_warnings.setText("⚠ XML 加载失败")
            self._log("ERR", f"XML 加载失败: {exc}")
            QMessageBox.critical(self, "XML 加载失败", f"{path}\n\n{exc}")
            return

        self._refresh_group_list()
        counts = self.device.point_counts()
        summary = (
            f"遥测 {counts['YC']} / 遥信 {counts['YX']} / "
            f"遥控 {counts['YK']} / 定值 {counts['FIX']}"
        )
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
        self.xml_warnings.setText("⚠ " + "；".join(warnings) if warnings else "")

    def _refresh_group_list(self) -> None:
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for group in self.device.sorted_groups():
            item = QListWidgetItem(group.title)
            item.setData(Qt.ItemDataRole.UserRole, group.key)
            item.setToolTip(
                f"功能码=0x{group.fun_type:04X} "
                f"类型={KIND_NAMES.get(group.kind, group.kind)} "
                f"点数={len(group.points)} 数据区={len(group.data)}B"
            )
            self.group_list.addItem(item)
        self.group_list.blockSignals(False)

        if self.group_list.count() > 0:
            self.group_list.setCurrentRow(0)
        else:
            self._current_group = None
            self._clear_detail()

    def _on_group_selected(self, item: QListWidgetItem | None, _previous=None) -> None:
        if item is None:
            self._current_group = None
            self._clear_detail()
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        group = self.device.groups.get(key) if isinstance(key, tuple) else None
        if group is None:
            self._current_group = None
            self._clear_detail()
            return
        self._current_group = group
        self._populate_group(group)

    # ----------------------------------------------------------------- table

    def _style_table(self, headers: list[str]) -> None:
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        widths = [240, 80, 70, 150, 110, 80, 110]
        for index in range(min(len(headers) - 1, len(widths))):
            self.table.setColumnWidth(index, widths[index])

    @staticmethod
    def _make_item(text: str, editable: bool = False) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _clear_detail(self) -> None:
        self.group_title.setText("未选择组")
        self.group_info.setText("")
        self.table.blockSignals(True)
        self.table.clear()
        self.table.clearSpans()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.table.blockSignals(False)
        self._row_specs = []

    def _populate_group(self, group: Group) -> None:
        self._current_group = group
        self._row_specs = []
        self.table.blockSignals(True)
        self.table.clear()
        self.table.clearSpans()
        self.table.setRowCount(0)

        self.group_title.setText(
            f"0x{group.fun_type:04X}  {KIND_NAMES.get(group.kind, group.kind)}"
        )
        self.group_info.setText(
            f"数据区 {len(group.data)} 字节 | XML 点 {len(group.points)} 个"
        )

        if group.kind == "YK":
            self._populate_yk(group)
        elif group.kind == "YX":
            self._populate_yx(group)
        else:
            self._populate_numeric(group)

        self.table.blockSignals(False)

    def _populate_numeric(self, group: Group) -> None:
        headers = [
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
        points = group.points
        gaps = self._unmapped_ranges(group)
        row_count = len(points) + len(gaps)
        self.table.setRowCount(max(1, row_count))
        self._row_specs = []

        if row_count == 0:
            item = self._make_item("该组没有数据区")
            self.table.setItem(0, 0, item)
            self.table.setSpan(0, 0, 1, len(headers))
            return

        for row, point in enumerate(points):
            self._row_specs.append(("point", point))
            if point.kind == "YX":
                byte_value = group.data[point.fun] if point.fun < len(group.data) else 0
                bit_value = (
                    (byte_value >> point.inf) & 0x01 if 0 <= point.inf < 8 else 0
                )
                self.table.setItem(row, 0, self._make_item(point.name))
                self.table.setItem(row, 1, self._make_item(str(point.fun)))
                self.table.setItem(row, 2, self._make_item("1 位"))
                self.table.setItem(row, 3, self._make_item(f"{byte_value:02X}"))
                self.table.setItem(row, 4, self._make_item(str(bit_value)))
                self.table.setItem(row, 5, self._make_item(""))
                self.table.setItem(row, 6, self._make_item(str(bit_value)))
                self.table.setItem(row, 7, self._make_item(f"bit {point.inf}"))
                continue

            raw = self._read_raw(group, point)
            raw_hex = format_hex(group.data[point.fun : point.fun + point.inf])
            factor = point.factor
            show_decimal = 0 < point.inf <= 4
            raw_dec = str(raw) if show_decimal else "-"
            engineering = f"{raw * factor:g}" if show_decimal else "-"
            note_parts = []
            if point.ied_dot:
                note_parts.append(f"ID={point.ied_dot}")
            if point.kind == "FIX" and point.value_type:
                note_parts.append(f"ValueType={point.value_type}")
            note = " | ".join(note_parts)

            self.table.setItem(row, 0, self._make_item(point.name or "(未命名)"))
            self.table.setItem(row, 1, self._make_item(str(point.fun)))
            self.table.setItem(row, 2, self._make_item(str(point.inf)))
            self.table.setItem(
                row, 3, self._make_item(raw_hex, editable=point.inf > 0)
            )
            self.table.setItem(row, 4, self._make_item(raw_dec, editable=show_decimal))
            self.table.setItem(
                row,
                5,
                self._make_item(f"{factor:g}", editable=point.kind == "FIX"),
            )
            self.table.setItem(
                row, 6, self._make_item(engineering, editable=show_decimal)
            )
            self.table.setItem(row, 7, self._make_item(note))

        for offset, (start, length) in enumerate(gaps, start=len(points)):
            self._row_specs.append(("ext", (start, length)))
            self._populate_extension_row(group, offset, start, length)

    def _populate_yx(self, group: Group) -> None:
        headers = ["名称", "字节", "位", "值", "说明"]
        self._style_table(headers)
        points = group.points
        self.table.setRowCount(max(1, len(points)))
        self._row_specs = []

        if not points:
            self.table.setItem(0, 0, self._make_item("无 XML 点定义"))
            self.table.setSpan(0, 0, 1, len(headers))
            return

        for row, point in enumerate(points):
            self._row_specs.append(("point", point))
            byte_value = group.data[point.fun] if point.fun < len(group.data) else 0
            checked = (
                bool((byte_value >> point.inf) & 0x01)
                if 0 <= point.inf < 8
                else False
            )
            self.table.setItem(row, 0, self._make_item(point.name))
            self.table.setItem(row, 1, self._make_item(str(point.fun)))
            self.table.setItem(row, 2, self._make_item(str(point.inf)))

            item = QTableWidgetItem()
            item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, 3, item)
            self.table.setItem(
                row,
                4,
                self._make_item(f"ID={point.ied_dot}" if point.ied_dot else ""),
            )

    def _populate_yk(self, group: Group) -> None:
        headers = ["名称", "功能码", "子类型", "应答结果", "说明"]
        self._style_table(headers)
        points = group.points
        self.table.setRowCount(max(1, len(points)))
        self._row_specs = []

        if not points:
            self.table.setItem(0, 0, self._make_item("未定义控制"))
            self.table.setItem(0, 1, self._make_item(f"0x{group.group_no:02X}"))
            self.table.setItem(0, 2, self._make_item(f"0x{group.entry_no:02X}"))
            self._add_result_combo(0, group)
            return

        for row, point in enumerate(points):
            self._row_specs.append(("point", point))
            self.table.setItem(row, 0, self._make_item(point.name))
            self.table.setItem(row, 1, self._make_item(f"0x{group.group_no:02X}"))
            self.table.setItem(row, 2, self._make_item(f"0x{group.entry_no:02X}"))
            self._add_result_combo(row, group)
            self.table.setItem(
                row,
                4,
                self._make_item(f"ID={point.ied_dot}" if point.ied_dot else ""),
            )

    def _add_result_combo(self, row: int, group: Group) -> None:
        combo = QComboBox()
        combo.addItem("成功 (0x01)", 0x01)
        combo.addItem("失败 (0x81)", 0x81)
        index = combo.findData(group.result)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.currentIndexChanged.connect(
            lambda _index, g=group, c=combo: self._on_yk_result_changed(g, c)
        )
        self.table.setCellWidget(row, 3, combo)

    def _on_yk_result_changed(self, group: Group, combo: QComboBox) -> None:
        if self._updating:
            return
        value = combo.currentData()
        group.result = int(value) if value is not None else 0x01
        if not group.data:
            group.data = bytearray((group.result,))
        else:
            group.data[0] = group.result

    # ----------------------------------------------------------- value edits

    def _read_raw(self, group: Group, point: Point) -> int:
        if point.inf <= 0:
            return 0
        end = point.fun + point.inf
        if end > len(group.data):
            group.data.extend(b"\x00" * (end - len(group.data)))
        return int.from_bytes(group.data[point.fun:end], "big")

    def _write_raw(self, group: Group, point: Point, raw: int) -> bool:
        if point.inf <= 0:
            return False
        max_raw = (1 << (8 * point.inf)) - 1
        if raw < 0 or raw > max_raw:
            self._log("ERR", f"{point.name} 超出范围 0..{max_raw}")
            return False
        end = point.fun + point.inf
        if end > len(group.data):
            group.data.extend(b"\x00" * (end - len(group.data)))
        group.data[point.fun:end] = int(raw).to_bytes(point.inf, "big")
        return True

    def _write_bit(self, group: Group, point: Point, checked: bool) -> None:
        if not (0 <= point.inf < 8):
            return
        if point.fun >= len(group.data):
            group.data.extend(b"\x00" * (point.fun + 1 - len(group.data)))
        mask = 1 << point.inf
        if checked:
            group.data[point.fun] = (group.data[point.fun] | mask) & 0xFF
        else:
            group.data[point.fun] = group.data[point.fun] & (~mask & 0xFF)

    def _refresh_numeric_row(self, group: Group, point: Point, row: int) -> None:
        self._updating = True
        self.table.blockSignals(True)
        try:
            raw = self._read_raw(group, point)
            raw_hex = format_hex(group.data[point.fun : point.fun + point.inf])
            factor = point.factor
            self.table.item(row, 3).setText(raw_hex)
            self.table.item(row, 4).setText(str(raw))
            self.table.item(row, 5).setText(f"{factor:g}")
            self.table.item(row, 6).setText(f"{raw * factor:g}")
        finally:
            self.table.blockSignals(False)
            self._updating = False

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or self._current_group is None:
            return

        group = self._current_group
        row = item.row()
        column = item.column()
        if row < 0 or row >= len(self._row_specs):
            return
        spec = self._row_specs[row]

        if group.kind == "YK":
            return

        if group.kind == "YX":
            if column != 3 or spec[0] != "point":
                return
            point = spec[1]
            self._write_bit(
                group, point, item.checkState() == Qt.CheckState.Checked
            )
            return

        if spec[0] == "ext":
            start, length = spec[1]
            if column == 3:
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
            elif column == 4:
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

        point = spec[1]
        if point.kind == "YX":
            return

        if column == 3:
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
            raw = int.from_bytes(data, "big") if data else 0
            if not self._write_raw(group, point, raw):
                self._refresh_numeric_row(group, point, row)
                return
        elif column == 4:
            try:
                raw = int(item.text().strip(), 0)
            except ValueError:
                self._log("ERR", f"原始值必须是整数: {item.text()}")
                self._refresh_numeric_row(group, point, row)
                return
            if not self._write_raw(group, point, raw):
                self._refresh_numeric_row(group, point, row)
                return
        elif column == 5:
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
        elif column == 6:
            try:
                value = float(item.text().strip())
            except ValueError:
                self._log("ERR", f"工程值必须是数字: {item.text()}")
                self._refresh_numeric_row(group, point, row)
                return
            factor = point.factor
            if factor == 0:
                self._log("ERR", "系数为 0，无法换算")
                self._refresh_numeric_row(group, point, row)
                return
            if not self._write_raw(group, point, round(value / factor)):
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

    def _populate_extension_row(
        self, group: Group, row: int, start: int, length: int
    ) -> None:
        raw = self._read_range(group, start, length)
        raw_hex = format_hex(group.data[start : start + length])
        show_decimal = 0 < length <= 4
        raw_dec = str(raw) if show_decimal else "-"
        name = "原始数据" if not group.points else "扩展字段"
        self.table.setItem(
            row,
            0,
            self._make_item(f"{name} [{start}..{start + length - 1}]"),
        )
        self.table.setItem(row, 1, self._make_item(str(start)))
        self.table.setItem(row, 2, self._make_item(str(length)))
        self.table.setItem(row, 3, self._make_item(raw_hex, editable=True))
        self.table.setItem(row, 4, self._make_item(raw_dec, editable=show_decimal))
        self.table.setItem(row, 5, self._make_item("1"))
        self.table.setItem(row, 6, self._make_item(raw_dec))
        self.table.setItem(row, 7, self._make_item("未映射字节，直接改原始值"))

    def _refresh_extension_row(
        self, group: Group, row: int, start: int, length: int
    ) -> None:
        self._updating = True
        self.table.blockSignals(True)
        try:
            raw = self._read_range(group, start, length)
            show_decimal = 0 < length <= 4
            raw_dec = str(raw) if show_decimal else "-"
            self.table.item(row, 3).setText(
                format_hex(group.data[start : start + length])
            )
            self.table.item(row, 4).setText(raw_dec)
            self.table.item(row, 6).setText(raw_dec)
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
        if self._current_group is not None:
            self._populate_group(self._current_group)
        self._log("INFO", "所有组已重置为模板/零值")

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
        self._append_log(self.event_view, self.event_autoscroll_cb, tag, text)

    def _log_frame(self, tag: str, text: str) -> None:
        self._append_log(self.frame_view, self.frame_autoscroll_cb, tag, text)

    @staticmethod
    def _append_log(
        view: QPlainTextEdit,
        autoscroll: QCheckBox,
        tag: str,
        text: str,
    ) -> None:
        timestamp = QDateTime.currentDateTime().toString("HH:mm:ss.zzz")
        view.appendPlainText(f"[{timestamp}] {tag:<5} {text}")
        if autoscroll.isChecked():
            bar = view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def closeEvent(self, event) -> None:
        self._save_settings()
        if self.port.isOpen():
            self.port.close()
        super().closeEvent(event)
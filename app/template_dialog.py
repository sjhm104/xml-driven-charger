"""组模板管理对话框。

支持：
- 直接编辑 组 / 数据区 HEX
- 从当前组生成
- 从完整报文导入（粘贴 EB90 响应帧）
- 从"录制响应"缓存导入
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.device_model import DeviceModel, Group, format_hex, parse_hex
from app.frame_codec import Frame, FrameCodec
from app.template_store import save_templates, template_key_for_frame


class TemplateDialog(QDialog):
    def __init__(
        self,
        parent,
        templates: dict[str, str],
        current_group: Group | None,
        recorded_frames: list[Frame],
        templates_path: str | Path,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("组模板管理")
        self.resize(820, 460)

        self._templates_path = Path(templates_path)
        self._current_group = current_group
        self._recorded_frames = list(recorded_frames)
        self.templates: dict[str, str] = dict(templates)
        self.apply_now = False

        self._build_ui()
        self._load_rows()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        tip = QLabel(
            "一行一个组。组支持 0x1B01 / 1B01 / 27/1（十进制）/ 0x1B,0x01；"
            "数据区是十六进制字节。备注只在本对话框显示，不写入 JSON。\n"
            "0x1A/0x1B/0x83 的请求和响应功能码相同，录制时按“请求后 20~280ms 内、"
            "同一功能码/子类型、有数据区”判断响应。"
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["组", "数据区 HEX", "备注"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 500)
        layout.addWidget(self.table, 1)

        row1 = QHBoxLayout()
        self.add_btn = QPushButton("新增行")
        self.del_btn = QPushButton("删除行")
        self.current_btn = QPushButton("从当前组生成")
        self.frame_btn = QPushButton("从报文导入...")
        self.recorded_btn = QPushButton("从录制导入...")
        row1.addWidget(self.add_btn)
        row1.addWidget(self.del_btn)
        row1.addSpacing(12)
        row1.addWidget(self.current_btn)
        row1.addWidget(self.frame_btn)
        row1.addWidget(self.recorded_btn)
        row1.addStretch(1)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addStretch(1)
        self.save_btn = QPushButton("保存")
        self.save_apply_btn = QPushButton("保存并应用")
        self.cancel_btn = QPushButton("取消")
        row2.addWidget(self.save_btn)
        row2.addWidget(self.save_apply_btn)
        row2.addWidget(self.cancel_btn)
        layout.addLayout(row2)

        self.add_btn.clicked.connect(self._on_add)
        self.del_btn.clicked.connect(self._on_delete)
        self.current_btn.clicked.connect(self._on_from_current)
        self.frame_btn.clicked.connect(self._on_from_frame)
        self.recorded_btn.clicked.connect(self._on_from_recorded)
        self.save_btn.clicked.connect(lambda: self._on_save(False))
        self.save_apply_btn.clicked.connect(lambda: self._on_save(True))
        self.cancel_btn.clicked.connect(self.reject)

    # ---------------------------------------------------------------- rows

    def _load_rows(self) -> None:
        self.table.setRowCount(0)
        for key in sorted(self.templates):
            try:
                normalized = self._normalize_key(key)
            except ValueError:
                normalized = key
            self._add_row(normalized, self.templates[key])

    @staticmethod
    def _normalize_key(text: str) -> str:
        group_no, entry_no = DeviceModel.parse_group_key(text)
        value = ((group_no & 0xFF) << 8) | (entry_no & 0xFF)
        return f"0x{value:04X}"

    def _add_row(self, key: str = "", data: str = "", note: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(key))
        self.table.setItem(row, 1, QTableWidgetItem(data))
        self.table.setItem(row, 2, QTableWidgetItem(note))

    def _upsert(self, key: str, data: str, note: str = "") -> None:
        key = self._normalize_key(key)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            if item.text().strip().upper() == key.upper():
                self.table.item(row, 1).setText(data)
                self.table.item(row, 2).setText(note)
                self.table.setCurrentCell(row, 0)
                return
        self._add_row(key, data, note)
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)

    def _on_add(self) -> None:
        self._add_row()
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)

    def _on_delete(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _on_from_current(self) -> None:
        group = self._current_group
        if group is None:
            QMessageBox.information(self, "提示", "当前没有选中的组")
            return
        key = f"0x{group.fun_type:04X}"
        self._upsert(key, format_hex(bytes(group.data)), "从当前组生成")

    def _on_from_frame(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self,
            "从报文导入",
            "粘贴完整响应帧 HEX（可带空格 / 0x）：",
        )
        if not ok or not text.strip():
            return
        try:
            frames = FrameCodec.extract(bytearray(parse_hex(text)))
        except ValueError:
            QMessageBox.warning(self, "解析失败", "HEX 格式不正确")
            return
        valid = [frame for frame in frames if frame.valid]
        if len(valid) != 1:
            QMessageBox.warning(
                self,
                "解析失败",
                f"需要且只需要 1 个完整帧，实际解析到 {len(valid)} 个有效帧",
            )
            return
        frame = valid[0]
        if not frame.data:
            QMessageBox.warning(self, "解析失败", "该帧没有数据区，不能作为模板")
            return
        key = template_key_for_frame(frame)
        if key is None:
            QMessageBox.warning(
                self,
                "解析失败",
                "无法从该帧推导组（仅支持 0x81/0x82/0x1A/0x1B/0x83 响应）",
            )
            return
        self._upsert(key, format_hex(frame.data), "从报文导入")

    def _on_from_recorded(self) -> None:
        if not self._recorded_frames:
            QMessageBox.information(
                self,
                "提示",
                "还没有录到响应。\n\n"
                "录制方法：取消“自动应答”，勾选“录制响应”，"
                "让 DTU 轮询真实充电机，工具只监听。",
            )
            return

        items: list[str] = []
        for index, frame in enumerate(self._recorded_frames, 1):
            key = template_key_for_frame(frame) or "?"
            items.append(
                f"{index}. {key}  {len(frame.data)}B  {frame.hex()[:70]}"
            )
        selected, ok = QInputDialog.getItem(
            self,
            "从录制导入",
            "最近录制的响应：",
            items,
            0,
            False,
        )
        if not ok:
            return
        frame = self._recorded_frames[items.index(selected)]
        key = template_key_for_frame(frame)
        if key is None:
            QMessageBox.warning(self, "提示", "无法从该帧推导组")
            return
        self._upsert(key, format_hex(frame.data), "从录制导入")

    # ---------------------------------------------------------------- save

    def _collect_templates(self) -> dict[str, str] | None:
        result: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            key_item = self.table.item(row, 0)
            data_item = self.table.item(row, 1)
            key_text = key_item.text().strip() if key_item else ""
            data_text = data_item.text().strip() if data_item else ""
            if not key_text and not data_text:
                continue
            if not key_text or not data_text:
                QMessageBox.warning(
                    self, "模板错误", f"第 {row + 1} 行：组和数据区都必须填写"
                )
                return None
            try:
                key = self._normalize_key(key_text)
            except ValueError:
                QMessageBox.warning(
                    self, "模板错误", f"第 {row + 1} 行：组格式错误: {key_text}"
                )
                return None
            try:
                data = parse_hex(data_text)
            except ValueError:
                QMessageBox.warning(
                    self, "模板错误", f"第 {row + 1} 行：HEX 格式错误"
                )
                return None
            if not data:
                QMessageBox.warning(
                    self, "模板错误", f"第 {row + 1} 行：数据区不能为空"
                )
                return None
            if key in result:
                QMessageBox.warning(
                    self, "模板错误", f"第 {row + 1} 行：组重复 {key}"
                )
                return None
            result[key] = format_hex(data)
        return result

    def _on_save(self, apply_now: bool) -> None:
        templates = self._collect_templates()
        if templates is None:
            return

        if apply_now:
            answer = QMessageBox.question(
                self,
                "应用模板",
                "保存后会按模板重置所有组的数据区，当前手动改的值会丢失。继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        try:
            save_templates(self._templates_path, templates)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        self.templates = templates
        self.apply_now = apply_now
        self.accept()
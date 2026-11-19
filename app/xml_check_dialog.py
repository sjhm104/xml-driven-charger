"""XML 校验结果对话框。"""

from __future__ import annotations

from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from app.xml_validator import Issue


class XmlCheckDialog(QDialog):
    def __init__(
        self,
        parent,
        xml_path: str,
        notes: list[str],
        issues: list[Issue],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("XML 校验")
        self.resize(820, 460)
        self._xml_path = xml_path
        self._notes = list(notes)
        self._issues = list(issues)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"XML: {xml_path}"))

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFont("Consolas", 9))
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.text, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.copy_btn = QPushButton("复制")
        self.close_btn = QPushButton("关闭")
        buttons.addWidget(self.copy_btn)
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)

        self.copy_btn.clicked.connect(self._copy)
        self.close_btn.clicked.connect(self.accept)
        self._render()

    def _render(self) -> None:
        lines: list[str] = []
        for note in self._notes:
            lines.append(f"[WARN] XML: {note}")
        for issue in self._issues:
            lines.append(str(issue))
        if not lines:
            lines.append("未发现明显问题。")
        self.text.setPlainText("\n".join(lines))

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.text.toPlainText())
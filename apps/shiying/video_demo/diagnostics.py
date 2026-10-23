"""On-demand failure help and a small, redacted report; no network or storage."""
from __future__ import annotations

import platform
import re
from importlib.metadata import version
from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit,
    QPushButton, QTextBrowser, QVBoxLayout,
)

from .errors import ErrorHint, redact_detail


def diagnostic_text(value):
    text = re.sub(r'''(?i)https?://[^\s<>"']+''', "[网址已隐藏]", str(value))
    text = redact_detail(text)
    # Credential errors may use key=value or dict representations, rather than
    # HTTP's `Cookie:` syntax. Drop the remainder of that line conservatively.
    text = re.sub(r'''(?im)\b(cookie|authorization|proxy-authorization|sessdata|sessionid|auth_token|access_token|refresh_token|password|token)["']?\s*[:=][^\r\n]*''',
                  r"\1: [已隐藏]", text)
    text = re.sub(r'''(?i)\b[A-Z]:[\\/][^\r\n<>|"]+''', "[本机路径]", text)
    return text


def build_report(hint: ErrorHint, operation, url, *, login="anonymous", proxy=False):
    try:
        host = urlsplit(url).hostname or "未提供"
    except ValueError:
        host = "未提供"
    operations = {"analyze": "查找视频", "analyze_batch": "批量查找", "download": "下载视频", "cookies": "读取浏览器", "restore_queue": "恢复队列"}
    logins = {"anonymous": "未使用登录信息", "file": "使用导入文件（不包含内容）", "session": "使用已读取的信息（不包含内容）"}
    lines = ["拾影 · 诊断信息", f"操作：{operations.get(operation, '处理视频')}",
             f"网站：{host}", f"错误类别：{hint.code}",
             f"系统：{platform.system()} {platform.release()}", f"Python：{platform.python_version()}",
             f"yt-dlp：{version('yt-dlp')}", f"PySide6：{version('PySide6')}",
             f"登录设置：{logins.get(login, logins['anonymous'])}",
             f"代理设置：{'软件内已填写（不包含地址）' if proxy else '跟随系统设置'}",
             "", hint.as_text(include_detail=True), "",
             "报告不含完整视频网址、登录信息内容、代理地址和本机完整路径。复制前可自行检查；不会自动发送。"]
    report = "\n".join(lines)
    if isinstance(proxy, str) and proxy:
        report = report.replace(proxy, "[代理地址已隐藏]")
        try:
            proxy_host = urlsplit(proxy).hostname
            if proxy_host:
                report = re.sub(re.escape(proxy_host) + r"(?::\d+)?", "[代理地址已隐藏]", report, flags=re.I)
        except ValueError:
            pass
    return diagnostic_text(report)


def suggested_action(hint, operation):
    if hint.code.startswith("browser_cookie_"):
        return "import", "改用文件或复制导入"
    if hint.code in {"authentication", "session_required", "session_expired", "membership_required", "audience_restricted"}:
        return "login", "设置登录信息"
    if hint.code in {"disk_full", "disk_low", "destination_unavailable", "file_too_large", "path_too_long", "save_permission", "progress_busy"}:
        return "folder", "更改保存位置"
    if hint.code in {"proxy", "dns", "tls", "connection"}:
        return "network", "检查网络设置"
    if hint.code in {"access_denied", "request_rejected"}:
        return "network", "登录与代理…"
    if operation == "download" and hint.code in {"quality_unavailable", "invalid_media", "not_detected", "unavailable", "source_changed"}:
        return "lookup", "重新查找原视频"
    return None


class DiagnosticDialog(QDialog):
    def __init__(self, title, message, hint, report, operation, parent=None, *, allow_public=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 550)
        self.action = None
        self.report = report
        layout = QVBoxLayout(self)
        content = QTextBrowser()
        content.setAccessibleName("失败原因和处理建议")
        content.setStyleSheet("QTextBrowser {background: white; border: 1px solid #dce3ef; border-radius: 8px; padding: 12px;}")
        content.setPlainText(message)
        layout.addWidget(content, 1)
        self.detail_toggle = QPushButton("展开详细错误")
        self.detail_toggle.setCheckable(True)
        layout.addWidget(self.detail_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.details = QPlainTextEdit(diagnostic_text(hint.detail))
        self.details.setAccessibleName("已隐藏敏感内容的详细错误")
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(135)
        self.details.hide()
        self.detail_toggle.toggled.connect(self.details.setVisible)
        self.detail_toggle.toggled.connect(lambda expanded: self.detail_toggle.setText("收起详细错误" if expanded else "展开详细错误"))
        layout.addWidget(self.details)
        self.note = QLabel("复制内容会隐藏网址、凭据和常见本机路径；发送前可以先粘贴检查，不会自动发送。")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox()
        self.action_buttons = {}
        action = suggested_action(hint, operation)
        if action:
            self.add_action(buttons, *action)
            if action[0] == "lookup":
                self.action_buttons["lookup"].setToolTip("重新读取这个视频的原网页；成功后会替换当前视频列表。")
        # Avoid encouraging repeated requests while a service is limiting access.
        if hint.code not in {"rate_limited", "protected", "dependency", "membership_required", "audience_restricted", "password_required"}:
            self.add_action(buttons, "retry", "重试这项操作")
        if allow_public and hint.code in {"authentication", "session_expired", "session_required", "access_denied", "request_rejected"}:
            self.add_action(buttons, "public", "按公开链接重试")
            self.action_buttons["public"].setToolTip("本轮不发送已添加的登录信息，原设置会保留。\n仅适合本来就能公开访问的视频；不会增加账号权限。")
        self.copy_button = buttons.addButton("复制诊断信息", QDialogButtonBox.ButtonRole.ActionRole)
        self.copy_button.clicked.connect(self.copy_report)
        close = buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        close.setDefault(True)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def add_action(self, buttons, action, text):
        button = buttons.addButton(text, QDialogButtonBox.ButtonRole.ActionRole)
        self.action_buttons[action] = button
        button.clicked.connect(lambda: self.choose(action))

    def choose(self, action):
        self.action = action
        self.accept()

    def copy_report(self):
        QApplication.clipboard().setText(self.report)
        self.note.setText("已复制诊断信息。发送前可以先粘贴检查；其中不包含完整网址或登录信息内容。")

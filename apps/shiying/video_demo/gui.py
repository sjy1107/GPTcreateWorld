from __future__ import annotations

import copy
import math
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QThread, QTimer, Qt, Signal, QTranslator, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLayout, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .core import MAX_VIDEOS, Cancelled, Network, Video, analyze, check_cancel, download, ffmpeg_path, validate_url
from .errors import explain_error, redact_detail
from .browser_cookies import BrowserCookieError, BrowserSession, browser_root, discover_profiles, domain_in_scope, load_cookie_file, inspect_cookie_file, inspect_cookie_text, inspect_cookie_jar, read_browser_session, site_scope
from .preferences import load_folder, save_folder
from .history import HistoryStore, video_identity
from .history_dialog import HistoryDialog
from .help_ui import HelpDialog, LOGIN_HELP, help_button, run_dialog
from .transfer import MAX_LINKS, bytes_label, estimate_sizes, estimate_space, shared_urls
from .cookie_guide import CookieGuide, downloads_folder
from .progress_dialog import ProgressDialog
from .diagnostics import DiagnosticDialog, build_report
from .queue_store import QueueStore, restore_queue, job_key
from .queue_dialog import QueueDialog
from .task_control import ItemControl
from .naming_dialog import NamingDialog
from .naming import filename_stem
from .storage import preflight_destination, volume_info
from .resume import list_progress
from .usage_guide import UsageGuide
from .result_notices import brief as notice_brief
from .result_notice_dialog import ResultNoticeDialog
from .system_dialog import SystemDialog, LocalJob
from .health import environment_checks
from . import preferences

ROOT = Path(__file__).resolve().parent.parent


def install_translation(app):
    if not hasattr(app, "_shiying_translator"):
        translator = QTranslator(app)
        if translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            app.installTranslator(translator)
        app._shiying_translator = translator


def duration_label(seconds):
    try:
        seconds = float(seconds)
    except (TypeError, ValueError, OverflowError):
        return "时长未知"
    if not math.isfinite(seconds) or seconds < 0:
        return "时长未知"
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


def speed_label(speed):
    if not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0:
        return "速度计算中"
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if speed < 1024 or unit == "GB/s":
            return f"{speed:.1f} {unit}"
        speed /= 1024


class Worker(QThread):
    event = Signal(str, object)

    def __init__(self, operation: str, payload, network: Network | None = None, session: BrowserSession | None = None, naming=None, history=None):
        super().__init__()
        self.operation, self.payload = operation, payload
        self.cancel = threading.Event()
        self.result = None
        self.error = ""
        self.error_hint = None
        self.cancelled = False
        self.network = network or Network()
        self.session = session
        self.last_progress = ("", 0.0)
        self.cookie_jar = None
        self.naming = naming
        self.history = history
        self.item_lock = threading.Lock()
        self.active_item = None
        self.active_row = None

    def can_skip(self, row):
        with self.item_lock:
            return bool(self.operation == "download" and row is not None and row == self.active_row
                        and self.active_item and not self.active_item.is_set())

    def request_skip(self, row):
        with self.item_lock:
            if (self.operation != "download" or row is None or row != self.active_row
                    or not self.active_item or self.active_item.is_set()):
                return False
            self.active_item.skip.set()
            return True

    def notify(self, kind, data):
        if kind == "current":
            self.last_progress = ("", 0.0)
        if kind == "progress":
            phase, stamp = self.last_progress
            now = time.monotonic()
            if phase == data["phase"] and now - stamp < 0.12 and data.get("percent") != 100:
                return
            self.last_progress = (data["phase"], now)
        self.event.emit(kind, data)

    def run(self):
        # yt-dlp may update its cookie jar. Work on a private temporary copy,
        # leaving the imported user file untouched and removing the copy on exit.
        try:
            if self.operation == "cookies":
                self.result = read_browser_session(*self.payload, check=lambda: check_cancel(self.cancel))
                return
            with tempfile.TemporaryDirectory(prefix="video-network-") as temp:
                self.network_temp = Path(temp)
                self.cookie_index = 0
                if self.network.cookie_file:
                    self.cookie_jar = load_cookie_file(self.network.cookie_file)
                self.perform(Network(self.network.proxy, "", self.network.rate_limit, self.network.keep_progress))
        except Exception as exc:
            self.fail(exc)
        finally:
            self.cookie_jar = None
            self.session = None

    def network_for_url(self, network, url):
        """Scope each page independently, including every item in a mixed batch."""
        scoped = Network(network.proxy, "", network.rate_limit, network.keep_progress)
        session = self.session if self.session and self.session.supports(url) else None
        if self.cookie_jar:
            roots = site_scope(url)
            cookies = tuple(copy.deepcopy(cookie) for cookie in self.cookie_jar
                            if domain_in_scope(cookie.domain, roots) and not cookie.is_expired())
            if cookies:
                session = BrowserSession("file", roots, cookies)
        if session:
            self.cookie_index += 1
            path = self.network_temp / f"cookies-{self.cookie_index}.txt"
            try:
                session.write_task_file(path)
                scoped.cookie_file = str(path)
            except BrowserCookieError as exc:
                if exc.code != "browser_cookie_expired":
                    raise
                self.notify("log", {"text": "已读取的会话已过期，本次先尝试公开资源；如站点要求登录，请重新读取登录状态。"})
        return scoped

    def fail(self, error):
        if self.cancel.is_set():
            self.cancelled = True
        else:
            self.error_hint = explain_error(error, self.operation)
            self.error = self.error_hint.as_text()

    def perform(self, network):
        try:
            if self.operation == "analyze":
                self.result = analyze(self.payload, self.cancel, self.notify, self.network_for_url(network, self.payload))
                return
            if self.operation == "analyze_batch":
                videos, failures, seen = [], [], set()
                processed = 0
                for index, url in enumerate(self.payload, 1):
                    check_cancel(self.cancel)
                    self.notify("lookup", {"index": index, "total": len(self.payload)})
                    try:
                        found = analyze(url, self.cancel, self.notify, self.network_for_url(network, url))
                        for video in found:
                            identity = video_identity(video)
                            if identity not in seen:
                                seen.add(identity)
                                videos.append(video)
                            if len(videos) >= MAX_VIDEOS:
                                break
                    except Cancelled:
                        raise
                    except Exception as exc:
                        check_cancel(self.cancel)
                        hint = explain_error(exc, "analyze")
                        failures.append({"url": url, "index": index, "hint": hint.as_text()})
                        self.notify("log", {"text": f"第 {index} 个链接查找失败：\n{hint.as_text()}"})
                    processed = index
                    if len(videos) >= MAX_VIDEOS:
                        break
                check_cancel(self.cancel)
                self.result = {"videos": videos, "failures": failures, "processed": processed,
                               "pending": self.payload[processed:]}
                return
            if self.operation == "restore_queue":
                self.result = restore_queue(self.payload,
                                            lambda url: analyze(url, self.cancel, self.notify, self.network_for_url(network, url)),
                                            self.cancel, self.notify)
                return
            items, folder = self.payload
            counts = {"ok": 0, "failed": 0, "skipped": 0, "cancelled": False}
            for number, (row, video, height) in enumerate(items, 1):
                check_cancel(self.cancel)
                control = ItemControl(self.cancel)
                with self.item_lock:
                    self.active_row, self.active_item = row, control
                self.notify("current", {"row": row, "text": f"{number}/{len(items)} · {video.title}"})
                self.event.emit("row", {"row": row, "state": "下载中"})
                try:
                    extra = self.naming.get(row, {}) if self.naming is not None else {}
                    path = download(video, height, folder, control, self.notify,
                                    self.network_for_url(network, video.referer), **extra)
                    counts["ok"] += 1
                    if self.history is not None:
                        try:
                            self.notify('progress', {'phase': '记录保存结果', 'percent': None})
                            self.history.record(video, height, path)
                        except (OSError, ValueError, sqlite3.Error):
                            self.notify('log', {'text': '视频已保存，但下载历史暂时写入失败；不影响视频文件。'})
                    self.event.emit("row", {"row": row, "state": "已完成", "path": str(path),
                                            'history_handled': self.history is not None})
                    self.event.emit("log", {"text": f"已保存：{path}"})
                except Exception as exc:
                    if self.cancel.is_set():
                        self.event.emit("row", {"row": row, "state": "已取消"})
                        raise Cancelled from exc
                    if control.skip.is_set():
                        counts["skipped"] += 1
                        self.event.emit("row", {"row": row, "state": "已跳过"})
                        self.notify("log", {"text": f"已跳过：{video.title}；继续后面的任务。"})
                        continue
                    if isinstance(exc, Cancelled):
                        self.event.emit("row", {"row": row, "state": "已取消"})
                        raise
                    counts["failed"] += 1
                    hint = explain_error(exc)
                    self.event.emit("row", {"row": row, "state": "失败", "error": hint.as_text(), "detail": hint.detail, "hint": hint})
                    self.event.emit("log", {"text": f"下载失败：{video.title}\n{hint.as_text(include_detail=True)}"})
                    if hint.code in {'disk_low', 'disk_full', 'destination_unavailable', 'save_permission', 'file_too_large', 'path_too_long', 'local_file'}:
                        remaining = items[number:]
                        counts.update(storage_stopped=True, pending=len(remaining))
                        for pending_row, _, _ in remaining:
                            self.event.emit('row', {'row': pending_row, 'state': '已取消',
                                                   'error': '保存空间或位置异常，本项尚未开始。处理后可点“重试未完成”。'})
                        break
                finally:
                    with self.item_lock:
                        self.active_row, self.active_item = None, None
            self.result = counts
        except Cancelled:
            self.cancelled = True
        except Exception as exc:
            self.fail(exc)


STYLE = """
QMainWindow, QDialog, QWidget#surface { background: #f5f7fb; }
QWidget { color: #25344b; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 14px; }
QLabel#brand { color: white; background: #3464dc; border-radius: 11px; font-size: 22px; font-weight: 700; }
QLabel#title { font-size: 22px; font-weight: 700; color: #16253c; }
QLabel#muted { color: #5d6b82; }
QLabel#section { font-size: 15px; font-weight: 700; color: #243650; }
QLabel#badge { background: #e9efff; color: #3760bc; border-radius: 12px; padding: 5px 13px; font-weight: 600; }
QLabel#badge[tone="success"] { background: #e4f4ed; color: #23734f; }
QLabel#badge[tone="error"] { background: #fff0e8; color: #a54d2d; }
QFrame#card { background: white; border: 1px solid #e1e7f0; border-radius: 12px; }
QLineEdit { background: #fafbfe; border: 1px solid #d8dfeb; border-radius: 7px; padding: 7px 10px; min-height: 20px; selection-background-color: #3464dc; }
QLineEdit:focus { border: 1px solid #3464dc; background: white; }
QPushButton { background: white; border: 1px solid #d8dfeb; border-radius: 7px; padding: 7px 12px; min-height: 20px; font-weight: 500; }
QPushButton:hover { background: #eef3ff; border-color: #a9bfe9; }
QPushButton:pressed { background: #e2ebfd; }
QPushButton#primary { color: white; background: #3464dc; border: 1px solid #3464dc; font-weight: 600; }
QPushButton#primary:hover { background: #2855c3; }
QPushButton#primary:pressed { background: #224baa; }
QPushButton:disabled { background: #f4f6f9; color: #9ca6b6; border-color: #e6eaf0; }
QPushButton#primary:disabled { background: #bccbf0; color: white; border-color: #bccbf0; }
QPushButton#helpButton { padding: 0; min-height: 0; color: #526b9c; border-radius: 14px; font-weight: 600; }
QTabWidget::pane { background: white; border: 1px solid #dce3ef; border-radius: 8px; }
QTabBar::tab { background: #edf1f8; border: 0; padding: 9px 18px; margin-right: 5px; }
QTabBar::tab:selected { background: #3464dc; color: white; }
QTableWidget { background: white; border: 0; gridline-color: #edf0f6; selection-background-color: #edf3ff; selection-color: #1d2b43; alternate-background-color: #fafbfe; }
QHeaderView::section { background: #f6f8fc; color: #687790; border: 0; border-bottom: 1px solid #e5eaf3; padding: 8px; font-weight: 500; }
QTableWidget::item { padding: 6px; border-bottom: 1px solid #edf0f6; }
QComboBox { background: #fafbfe; border: 1px solid #dce3ef; border-radius: 6px; padding: 5px 8px; min-height: 20px; min-width: 100px; }
QComboBox::drop-down { border: 0; width: 22px; }
QComboBox QAbstractItemView { background: white; selection-background-color: #e5ecff; selection-color: #1d2b43; }
QProgressBar { border: 0; border-radius: 4px; background: #eaf0fa; max-height: 8px; min-height: 8px; }
QProgressBar::chunk { background: #3464dc; border-radius: 4px; }
QPlainTextEdit { background: #f8faff; color: #586983; border: 1px solid #e0e7f0; border-radius: 7px; padding: 6px; font-size: 12px; }
QCheckBox { spacing: 7px; }
QScrollArea { border: 0; }
QToolTip { background: #243650; color: white; padding: 6px; border: 0; }
"""


class StorageInfoWorker(QThread):
    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self.result = None
        self.error = ''

    def run(self):
        try:
            free = shutil.disk_usage(self.folder).free
            records = list_progress(self.folder)
            self.result = (free, sum(record['bytes'] for record in records), volume_info(self.folder).filesystem)
        except (OSError, ValueError, RuntimeError) as exc:
            self.error = str(exc)


class QualityCombo(QComboBox):
    def __init__(self, prepare):
        super().__init__()
        self.prepare = prepare

    def showPopup(self):
        self.prepare()
        self.view().setMinimumWidth(270)
        super().showPopup()


class MainWindow(QMainWindow):
    def __init__(self, history_path=None, queue_path=None):
        super().__init__()
        install_translation(QApplication.instance())
        self.videos: list[Video] = []
        self.states: dict[int, str] = {}
        self.paths: dict[int, str] = {}
        self.worker: Worker | None = None
        self.storage_worker = None
        self.health_worker = None
        self.health_results = []
        self.system_dialog = None
        self.current_row = None
        self.close_pending = False
        self.browser_session: BrowserSession | None = None
        self.history = HistoryStore(history_path)
        self.queue_store = QueueStore(queue_path or (Path(history_path).with_name("queue.json") if history_path else None))
        self.queue_unresolved = []
        self.queue_issues = []
        self.custom_names = {}
        self.naming_mode = self.queue_store.snapshot.get("naming_mode", "title_quality")
        self.queue_timer = QTimer(self)
        self.queue_timer.setSingleShot(True)
        self.queue_timer.timeout.connect(self.persist_queue)
        self.size_cache = {}
        self.size_timer = QTimer(self)
        self.size_timer.setSingleShot(True)
        self.size_timer.timeout.connect(self.refresh_size_hints)
        self.import_summary = ""
        self.import_site = None
        self.cookie_validation_key = None
        self.error_context = None
        self.results_url = ""
        self.batch_urls = []
        self.failed_urls = []
        self.batch_failures = []
        self.batch_pending = []
        self.populating = False
        self.setWindowTitle("拾影 · 视频下载")
        self.resize(1040, 790)
        self.setMinimumSize(880, 650)
        screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.resize(min(1040, max(880, area.width() - 48)), min(790, max(650, area.height() - 60)))
        self.setStyleSheet(STYLE)
        center = QWidget()
        center.setObjectName("surface")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(center)
        self.setCentralWidget(scroll)
        layout = QVBoxLayout(center)
        self.main_layout = layout
        self.compact_layout = None
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        mark = QLabel("影")
        self.brand_mark = mark
        mark.setObjectName("brand")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(46, 46)
        heading.addWidget(mark)
        brand = QVBoxLayout()
        brand.setSpacing(1)
        title = QLabel("拾影")
        title.setObjectName("title")
        brand.addWidget(title)
        brand.addWidget(self.muted("把网页里的视频，保存到电脑。"))
        heading.addLayout(brand, 1)
        self.history_button = QPushButton("下载历史")
        self.history_button.clicked.connect(self.show_history)
        heading.addWidget(self.history_button)
        self.quick_help = QPushButton('使用指南')
        self.quick_help.setAccessibleName('使用指南')
        self.quick_help.setToolTip('按 F1 或点击：按主题查找操作步骤，也可搜索不懂的词。\n普通视频先直接尝试，不需要设置登录信息。')
        self.quick_help.setShortcut('F1')
        self.quick_help.clicked.connect(self.show_quick_help)
        heading.addWidget(self.quick_help)
        self.system_button = QPushButton('检查与数据')
        self.system_button.setToolTip('查看启动环境、本机记录位置和占用；清除前会说明影响，不会自动删除视频。')
        self.system_button.clicked.connect(self.show_system)
        heading.addWidget(self.system_button)
        self.phase_badge = QLabel("准备就绪")
        self.phase_badge.setObjectName("badge")
        heading.addWidget(self.phase_badge)
        layout.addLayout(heading)

        source_card, source_layout = self.card()
        source_row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("粘贴视频网址或分享文字，也支持 MP4 等视频直链")
        self.url.setAccessibleName("视频网页地址")
        self.url.returnPressed.connect(self.start_analysis)
        self.paste_button = QPushButton("粘贴")
        self.paste_button.setToolTip("粘贴剪贴板中的网址；不会自动下载。")
        self.paste_button.clicked.connect(self.paste_url)
        self.batch_button = QPushButton("批量链接…")
        self.batch_button.clicked.connect(self.show_batch_input)
        self.analyze_button = QPushButton("查找视频")
        self.analyze_button.setObjectName("primary")
        self.analyze_button.clicked.connect(self.start_analysis)
        source_row.addWidget(self.url, 1)
        source_row.addWidget(self.paste_button)
        source_row.addWidget(self.batch_button)
        source_row.addWidget(self.analyze_button)
        source_layout.addLayout(source_row)
        source_options = QHBoxLayout()
        source_options.addWidget(self.section("1 · 添加链接"))
        self.source_hint = self.muted("公开链接无需登录")
        source_options.addWidget(self.source_hint, 1)
        self.lookup_report_button = QPushButton("查找情况")
        self.lookup_report_button.clicked.connect(self.show_lookup_report)
        self.lookup_report_button.hide()
        source_options.addWidget(self.lookup_report_button)
        self.network_toggle = QPushButton("登录与代理…")
        self.network_toggle.setCheckable(True)
        source_options.addWidget(self.network_toggle)
        source_layout.insertLayout(0, source_options)
        self.network_panel = QDialog(self)
        self.network_panel.setWindowTitle("登录与代理 · 按需设置")
        self.network_panel.setWindowModality(Qt.WindowModality.WindowModal)
        self.network_panel.setMinimumWidth(650)
        net_layout = QVBoxLayout(self.network_panel)
        net_layout.setContentsMargins(22, 20, 22, 20)
        net_layout.setSpacing(12)
        net_layout.addWidget(self.section("普通视频无需登录设置"))
        self.login_tabs = QTabWidget()
        browser_pane, file_pane = QWidget(), QWidget()
        browser_layout, file_layout = QVBoxLayout(browser_pane), QVBoxLayout(file_pane)
        browser_layout.setContentsMargins(14, 14, 14, 14)
        file_layout.setContentsMargins(14, 14, 14, 14)
        browser_layout.setSpacing(10)
        file_layout.setSpacing(10)
        self.login_tabs.addTab(browser_pane, "读取浏览器")
        self.login_tabs.addTab(file_pane, "文件导入")
        net_layout.addWidget(self.login_tabs)
        browser_layout.addWidget(self.muted("先确认这个浏览器能播放目标视频，再读取对应用户的登录状态。"))
        self.proxy = QLineEdit()
        self.proxy.setPlaceholderText("留空使用系统设置，例如 http://127.0.0.1:7890")
        self.proxy.setAccessibleName("可选代理地址")
        self.proxy.setToolTip("不使用代理就留空；已有系统代理时通常无需填写。\n需要填写时使用完整地址，例如 http://127.0.0.1:7890。")
        cookies_row = QHBoxLayout()
        self.cookies = QLineEdit()
        self.cookies.setPlaceholderText("可选：导入 Cookies .txt 或 .json 文件")
        self.cookies.setAccessibleName("可选 Cookies 文件")
        cookies_row.addWidget(self.cookies, 1)
        self.cookies_browse = QPushButton("选择 Cookies 文件")
        self.cookies_browse.clicked.connect(self.browse_cookies)
        cookies_row.addWidget(self.cookies_browse)
        self.cookie_file_help = help_button("内部数据库和导出文本有什么区别？", "文件通常在浏览器的“下载”文件夹，Ctrl+J 可查看实际位置。\n浏览器内部的 Cookies 数据库不能直接导入。\n点击查看详细排错说明；第一次操作建议点“按步骤获取 Cookies”。", self.show_cookie_file_help)
        self.cookie_guide_button = QPushButton("按步骤获取 Cookies…")
        self.cookie_guide_button.setObjectName("primary")
        self.cookie_guide_button.clicked.connect(self.show_cookie_guide)
        self.cookie_clipboard = QPushButton("从剪贴板导入")
        self.cookie_clipboard.clicked.connect(self.import_clipboard_cookies)
        browser_row = QHBoxLayout()
        self.cookie_browser = QComboBox()
        for label, name in (("Firefox（建议）", "firefox"), ("Edge", "edge"), ("Chrome", "chrome"), ("Brave", "brave")):
            self.cookie_browser.addItem(label, name)
        self.cookie_browser.setAccessibleName("读取登录状态的浏览器")
        self.read_cookies = QPushButton("读取当前网站登录状态")
        self.read_cookies.clicked.connect(self.start_cookie_read)
        self.clear_cookies = QPushButton("清除登录信息")
        self.clear_cookies.clicked.connect(self.clear_login_information)
        browser_row.addWidget(self.cookie_browser)
        browser_row.addWidget(self.read_cookies)
        browser_row.addWidget(self.clear_cookies)
        browser_row.addStretch()
        browser_row.addWidget(help_button("为什么可能读取失败？", "Edge / Chrome 可能因后台占用或加密而读取失败。\n点击查看解决方式；也可以切换到“文件导入”。", self.show_login_help))
        browser_layout.addLayout(browser_row)
        self.browser_notice = self.muted("")
        browser_layout.addWidget(self.browser_notice)
        profiles_row = QHBoxLayout()
        self.profile_picker = QComboBox()
        self.profile_picker.setMinimumWidth(230)
        self.profile_picker.currentIndexChanged.connect(self.choose_detected_profile)
        self.profile_refresh = QPushButton("检测浏览器用户")
        self.profile_refresh.clicked.connect(self.refresh_profiles)
        profiles_row.addWidget(self.profile_picker, 1)
        profiles_row.addWidget(self.profile_refresh)
        profiles_row.addWidget(help_button("怎么选浏览器用户？", "选择实际登录过这个网站的用户。\nEdge：在地址栏打开 edge://version，查看“个人资料路径”。\n检测操作只查看资料文件夹，不读取 Cookies 内容。", self.show_login_help))
        browser_layout.addLayout(profiles_row)
        self.browser_profile = QLineEdit()
        self.browser_profile.setPlaceholderText("通常留空；有多个浏览器用户时可填 Default、Profile 1 或资料文件夹")
        self.browser_profile.setToolTip("选实际登录过网站的那个浏览器用户。Firefox 可填资料文件夹完整路径。")
        self.browser_profile.setAccessibleName("可选浏览器用户资料")
        self.manual_profile_toggle = QCheckBox("手动指定资料位置（可选）")
        browser_layout.addWidget(self.manual_profile_toggle)
        manual_profile = QWidget()
        profile_row = QHBoxLayout()
        profile_row.setContentsMargins(0, 0, 0, 0)
        manual_profile.setLayout(profile_row)
        profile_row.addWidget(self.browser_profile, 1)
        profile_browse = QPushButton("选择资料文件夹")
        profile_browse.clicked.connect(self.browse_profile)
        profile_row.addWidget(profile_browse)
        browser_layout.addWidget(manual_profile)
        manual_profile.hide()
        self.manual_profile_toggle.toggled.connect(manual_profile.setVisible)
        self.cookie_status = self.muted("尚未使用登录信息。普通视频可以直接查找。")
        net_layout.addWidget(self.cookie_status)
        self.use_login = QCheckBox("使用已添加的登录信息")
        self.use_login.setChecked(True)
        self.use_login.setToolTip("取消勾选后，本轮查找和下载按公开访问尝试，不发送导入的 Cookies。\n原来的设置仍保留，重新勾选即可恢复；不会退出浏览器账号。")
        self.use_login.toggled.connect(self.update_cookie_status)
        net_layout.addWidget(self.use_login)
        self.url.textChanged.connect(self.update_cookie_status)
        self.cookies.textEdited.connect(self.clear_browser_session)
        self.cookies.textChanged.connect(self.update_cookie_status)
        self.cookies.textChanged.connect(lambda _: self.update_actions())
        file_layout.addWidget(self.muted("没有 Cookies 文件？点“按步骤获取 Cookies”，跟着做即可。\n也可在导出扩展里点 Copy，再回这里从剪贴板导入，免去找文件。"))
        file_layout.addLayout(cookies_row)
        file_help_row = QHBoxLayout()
        file_help_row.addWidget(self.cookie_guide_button)
        file_help_row.addWidget(self.cookie_clipboard)
        file_help_row.addStretch()
        file_help_row.addWidget(self.cookie_file_help)
        file_layout.addLayout(file_help_row)
        file_layout.addWidget(self.muted("支持 Netscape 文本及常见 JSON 导出列表，会自动识别格式。不要选择浏览器内部的 Cookies 数据库。"))
        file_layout.addStretch()
        self.proxy_toggle = QPushButton("展开代理设置（可选）")
        self.proxy_toggle.setCheckable(True)
        self.proxy_toggle.setToolTip("展开或收起输入框，不会改变已经填写的代理。\n清空输入框即可恢复系统设置。")
        self.proxy_toggle.toggled.connect(self.proxy.setVisible)
        self.proxy.textChanged.connect(lambda value: self.proxy_toggle.setText("代理设置（已填写）" if value.strip() else "展开代理设置（可选）"))
        net_layout.addWidget(self.proxy_toggle)
        net_layout.addWidget(self.proxy)
        self.proxy.hide()
        help_row = QHBoxLayout()
        help_row.addWidget(self.muted("读取结果仅在本机暂存。不要把登录信息或 Cookies 文件发给别人。"), 1)
        self.login_help = QPushButton("使用帮助")
        self.login_help.clicked.connect(self.show_login_help)
        help_row.addWidget(self.login_help)
        net_layout.addLayout(help_row)
        settings_buttons = QDialogButtonBox()
        self.cookie_check = settings_buttons.addButton("检查导入信息", QDialogButtonBox.ButtonRole.ActionRole)
        self.cookie_check.setToolTip("在本机检查当前网址对应的 Cookies、过期情况及常见登录标记。\n不会连接网站，也不会显示 Cookies 内容或确认账号权限。")
        self.cookie_check.clicked.connect(self.check_login_information)
        self.settings_lookup = settings_buttons.addButton("完成并查找视频", QDialogButtonBox.ButtonRole.ActionRole)
        self.settings_lookup.setObjectName("primary")
        self.settings_lookup.setToolTip("关闭设置，并查找主界面填写的网址。\n成功后替换当前视频列表，不会自动下载；不会自动重试整批链接。")
        self.settings_lookup.clicked.connect(self.lookup_from_settings)
        settings_buttons.addButton("完成", QDialogButtonBox.ButtonRole.AcceptRole)
        settings_buttons.accepted.connect(self.network_panel.accept)
        net_layout.addWidget(settings_buttons)
        self.network_panel.hide()
        self.network_toggle.toggled.connect(self.network_panel.setVisible)
        self.network_panel.finished.connect(lambda _: self.network_toggle.setChecked(False))
        layout.addWidget(source_card)

        results_card, results_layout = self.card()
        result_header = QHBoxLayout()
        self.count_label = self.section("2 · 选择视频")
        result_header.addWidget(self.count_label, 1)
        self.select_all = QCheckBox("全选")
        self.select_all.setChecked(True)
        self.select_all.toggled.connect(self.toggle_all)
        result_header.addWidget(self.select_all)
        self.naming_button = QPushButton("文件命名…")
        self.naming_button.setToolTip("选择标题或标题加画质，也可为每个视频填写文件名；不重命名已有文件。")
        self.naming_button.clicked.connect(self.show_naming)
        result_header.addWidget(self.naming_button)
        self.notice_button = QPushButton('解析提示…')
        self.notice_button.setToolTip('查看是否只有试看、画质是否缺失，以及所属列表有没有未解析条目。不能确认时会明确说明。')
        self.notice_button.clicked.connect(lambda: self.show_result_notices())
        result_header.addWidget(self.notice_button)
        self.batch_quality = QComboBox()
        self.batch_quality.addItem("批量设置画质", "keep")
        self.batch_quality.setToolTip("修改所有已勾选视频的画质；也可以逐行选择。")
        self.batch_quality.currentIndexChanged.connect(self.apply_batch_quality)
        result_header.addWidget(self.batch_quality)
        results_layout.addLayout(result_header)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["选择", "视频名称", "时长", "清晰度", "状态", "解析提示"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        for column, width in ((0, 52), (2, 80), (3, 190), (4, 112)):
            self.table.setColumnWidth(column, width)
        self.table.setColumnWidth(5, 130)
        self.table.setMinimumHeight(110)
        self.table.cellDoubleClicked.connect(self.open_video)
        self.table.itemChanged.connect(self.sync_selection)
        self.results_stack = QStackedWidget()
        blank = QWidget()
        blank_layout = QVBoxLayout(blank)
        blank_layout.addStretch()
        blank_title = self.section("从一个链接开始")
        blank_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        blank_layout.addWidget(blank_title)
        self.blank_hint = self.muted("粘贴视频网页，点击“查找视频”。找到后可选择画质，保存为 MP4。")
        self.blank_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        blank_layout.addWidget(self.blank_hint)
        blank_layout.addStretch()
        self.results_stack.addWidget(blank)
        self.results_stack.addWidget(self.table)
        self.results_stack.setMinimumHeight(120)
        results_layout.addWidget(self.results_stack, 1)
        summary = QHBoxLayout()
        self.empty_hint = self.muted("每次最多查找 50 个视频，部分网页可能无法识别。")
        summary.addWidget(self.empty_hint, 1)
        self.selection_label = self.muted("尚未找到视频")
        self.selection_label.setWordWrap(False)
        summary.addWidget(self.selection_label)
        results_layout.addLayout(summary)
        layout.addWidget(results_card, 1)

        download_card, download_layout = self.card()
        destination = QHBoxLayout()
        destination.addWidget(QLabel("3 · 保存到"))
        self.folder = QLineEdit(load_folder(ROOT / "downloads"))
        self.folder.setAccessibleName("保存文件夹")
        destination.addWidget(self.folder, 1)
        self.browse_button = QPushButton("更改位置")
        self.browse_button.clicked.connect(self.browse)
        destination.addWidget(self.browse_button)
        self.open_button = QPushButton("打开文件夹")
        self.open_button.clicked.connect(self.open_folder)
        destination.addWidget(self.open_button)
        download_layout.addLayout(destination)
        storage_row = QHBoxLayout()
        self.storage_hint = self.muted('剩余空间与未完成素材占用：点击“刷新占用”查看。')
        self.storage_hint.setWordWrap(True)
        self.storage_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.storage_hint.setToolTip('只检查当前保存位置及本软件的未完成素材，不扫描整个磁盘。下载时约每两秒检查空间；界面显示的是最近一次读数。')
        storage_row.addWidget(self.storage_hint, 1)
        self.storage_refresh = QPushButton('刷新占用')
        self.storage_refresh.setToolTip('按需在后台读取剩余空间和未完成素材大小；不清理任何文件。')
        self.storage_refresh.clicked.connect(self.refresh_storage_info)
        storage_row.addWidget(self.storage_refresh)
        download_layout.addLayout(storage_row)
        action_row = QHBoxLayout()
        self.download_button = QPushButton("下载选中视频")
        self.download_button.setObjectName("primary")
        self.download_button.clicked.connect(self.start_download)
        self.retry_button = QPushButton("重试未完成")
        self.retry_button.setToolTip("继续或重新下载失败、已取消或已跳过的视频；已成功保存的不会重下。\n勾选保留进度时，会先核对能否接着下载。")
        self.retry_button.clicked.connect(lambda: self.start_download(retry_only=True))
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.clicked.connect(self.cancel_task)
        self.skip_button = QPushButton("跳过当前项")
        self.skip_button.setToolTip("跳过正在下载或整理的视频，继续后面的任务。\n已保存的文件不会撤回，跳过的项目之后可重试。网络请求未响应时需要稍等。")
        self.skip_button.clicked.connect(self.skip_current)
        self.error_button = QPushButton("失败原因")
        self.error_button.clicked.connect(self.show_failure)
        action_row.addWidget(self.download_button)
        action_row.addWidget(self.retry_button)
        action_row.addWidget(self.skip_button)
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.error_button)
        self.keep_progress = QCheckBox("保留进度")
        self.keep_progress.setChecked(True)
        action_row.addStretch()
        self.rate_limit = QComboBox()
        for label, value in (("不限速", 0), ("限速 512 KB/s", 512 * 1024), ("限速 1 MB/s", 1024**2),
                             ("限速 2 MB/s", 2 * 1024**2), ("限速 5 MB/s", 5 * 1024**2)):
            self.rate_limit.addItem(label, value)
        self.rate_limit.setAccessibleName("下载速度上限")
        action_row.addWidget(self.rate_limit)
        download_layout.addLayout(action_row)
        self.task_label = QLabel("等待添加视频链接")
        self.task_label.setTextFormat(Qt.TextFormat.PlainText)
        self.task_label.setWordWrap(True)
        self.task_label.setTextFormat(Qt.TextFormat.PlainText)
        self.task_label.setMaximumHeight(42)
        download_layout.addWidget(self.task_label)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setValue(0)
        download_layout.addWidget(self.progress)
        self.progress_label = self.muted("找到视频后，勾选需要的项目，再点击下载。")
        download_layout.addWidget(self.progress_label)
        layout.addWidget(download_card)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(75)
        self.log.setAccessibleName("任务日志")
        self.log_toggle = QCheckBox("查看运行记录")
        self.log_toggle.toggled.connect(self.log.setVisible)
        footer_row = QHBoxLayout()
        footer_row.addWidget(self.log_toggle)
        self.progress_files_button = QPushButton("未完成文件…")
        self.progress_files_button.clicked.connect(self.show_progress_files)
        footer_row.addWidget(self.progress_files_button)
        self.queue_button = QPushButton("任务队列…")
        self.queue_button.setToolTip("可选：在本机记住未完成的视频，重启后重新查找恢复。默认不保存网址。")
        self.queue_button.clicked.connect(self.show_queue)
        footer_row.addWidget(self.queue_button)
        footer_row.addWidget(self.keep_progress)
        footer_row.addStretch()
        footer_hint = self.muted("已完成的视频可双击播放")
        footer_hint.setWordWrap(False)
        footer_row.addWidget(footer_hint)
        layout.addLayout(footer_row)
        self.log.hide()
        layout.addWidget(self.log)
        self.card_layouts = (source_layout, results_layout, download_layout)
        self.adjust_layout_density()
        self.url.textChanged.connect(self.source_changed)
        self.folder.textChanged.connect(self.schedule_queue_save)
        self.folder.textChanged.connect(lambda: self.storage_hint.setText('保存位置已更改，点击“刷新占用”查看空间与未完成素材。'))
        self.cookie_browser.currentIndexChanged.connect(self.browser_changed)
        for index in range(self.cookie_browser.count()):
            if discover_profiles(self.cookie_browser.itemData(index)):
                self.cookie_browser.setCurrentIndex(index)
                break
        self.refresh_profiles()
        self.setup_tooltips()
        self.update_actions()
        self.refresh_queue_button()
        if self.queue_store.snapshot["items"]:
            self.task_label.setText(f"有 {len(self.queue_store.snapshot['items'])} 项未完成任务。点击下方“任务队列”重新查找并恢复。")
        if self.queue_store.load_error:
            self.log.appendPlainText("记住的队列暂时无法读取，可在“任务队列”中清除后重新开启；视频文件不受影响。")
        self.log.appendPlainText('环境与组件的实际运行检查可在“检查与数据”查看；启动时会在后台执行。')

    @staticmethod
    def muted(text):
        label = QLabel(text)
        label.setObjectName("muted")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "card_layouts"):
            self.adjust_layout_density()

    def adjust_layout_density(self):
        compact = self.height() < 760
        if compact == self.compact_layout:
            return
        self.compact_layout = compact
        self.main_layout.setContentsMargins(*(16, 8, 16, 8) if compact else (22, 18, 22, 16))
        self.main_layout.setSpacing(6 if compact else 12)
        for layout in self.card_layouts:
            layout.setContentsMargins(*(14, 7, 14, 7) if compact else (16, 12, 16, 12))
            layout.setSpacing(4 if compact else 9)
        self.brand_mark.setFixedSize(40 if compact else 46, 40 if compact else 46)
        self.results_stack.setMinimumHeight(88 if compact else 120)
        self.table.setMinimumHeight(68 if compact else 110)

    @staticmethod
    def section(text):
        label = QLabel(text)
        label.setObjectName("section")
        return label

    @staticmethod
    def card():
        frame = QFrame()
        frame.setObjectName("card")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(16, 12, 16, 12)
        inner.setSpacing(9)
        return frame, inner

    def set_phase(self, text, tone=""):
        self.phase_badge.setText(text)
        self.phase_badge.setProperty("tone", tone)
        self.phase_badge.style().unpolish(self.phase_badge)
        self.phase_badge.style().polish(self.phase_badge)
        self.phase_badge.setMinimumWidth(self.phase_badge.fontMetrics().horizontalAdvance(text) + 30)
        self.phase_badge.updateGeometry()

    def setup_tooltips(self):
        for widget, text in (
            (self.url, "可粘贴完整网址或平台分享文字，多个链接会转到批量输入供你确认。\n也支持媒体直链。换了链接需重新查找，下方旧结果会暂时保留。"),
            (self.analyze_button, "先查找网页里的视频和可用画质，不会立即下载。最多识别 50 个视频。"),
            (self.batch_button, f"打开输入框，每行一个网址，一次最多 {MAX_LINKS} 个。\n逐个查找、合并去重；某个链接失败不会阻止其他链接。"),
            (self.rate_limit, "限制本次下载的传输速度，避免占满网络；下次下载前可修改。\n不影响查找网页、合并或转码；瞬时速度可能有波动。"),
            (self.network_toggle, "普通视频无需设置。网站要求登录或需要指定代理时再打开。"),
            (self.read_cookies, "读取软件里粘贴网址对应的网站，不会查看浏览器当前标签页。\n先确认选中的浏览器用户已登录且能播放视频。"),
            (self.cookies, "请选择导出的 Netscape / cookies.txt 文本或常见 Cookies JSON 列表。\n不是浏览器目录里的 Cookies 数据库；软件会自动识别导出格式。"),
            (self.cookie_guide_button, "按四步说明操作：确认能播放 → 准备导出工具 → 复制或导出 → 导入检查。\n内置商店入口和下载文件夹入口，无需寻找浏览器数据库。"),
            (self.cookie_clipboard, "在视频网站打开导出扩展，点 Copy 后再点这里。支持 Netscape 或常见 JSON 格式。\n只在点击时读取剪贴板；格式或网站不对时保留原来的登录设置。"),
            (self.profile_picker, "浏览器里有多个用户时，需选择实际登录过目标网站的那个。\nDefault 不一定是你当前使用的用户。"),
            (self.profile_refresh, "只检测默认资料目录中的用户文件夹，不读取登录信息。便携版可手动选文件夹。"),
            (self.clear_cookies, "清除软件暂存的登录信息和所选文件路径。\n不会退出浏览器账号，也不会删除原 Cookies 文件。"),
            (self.folder, "只记住保存目录，不记住网址或登录凭据。重名文件会自动区分。"),
            (self.cancel_button, "查找时停止查找；下载时结束当前视频及后面的整批任务。已完成的 MP4 会保留。\n只想继续后面的视频，请点“跳过当前项”。勾选“保留进度”时，符合条件的素材会留下供下次续传。"),
            (self.keep_progress, "中断后保留能继续使用的素材；不自动保存原网址。\n重试原视频，选择相同画质与保存位置，软件会自动核对能否续传。\n部分网站不能续传，资源变化时会重新下载。旧素材可在“未完成文件…”查看和清理。"),
            (self.progress_files_button, "查看当前保存位置下保留的未完成素材及占用空间，可勾选清理。\n不会删除已完成的 MP4；任务运行期间不可清理。"),
            (self.progress, "这是当前视频当前阶段的进度；下载、声音合并和整理 MP4 会分阶段显示。"),
            (self.progress_label, "速度和剩余时间针对当前传输，不是整个队列。\n等待重试时会显示原因和倒计时，可以取消或跳过当前下载。\n显示“整理为 MP4”时，请等到列表状态变为“已完成”。"),
            (self.error_button, "选中失败的视频后点击。可查看可能原因、建议及 HTTP 错误代码。"),
            (self.history_button, "搜索和打开成功下载的视频。\n记录仅在本机保存；清空记录不会删除视频，不保存原链接或登录信息。"),
        ):
            widget.setToolTip(text)

    def guide_availability(self):
        actions = ('source', 'batch', 'quality', 'storage', 'naming', 'controls', 'failure', 'queue', 'login', 'history', 'partials', 'proxy', 'health', 'data', 'engine', 'notices')
        if self.worker:
            return {action: '任务正在进行，可以先阅读说明；任务结束后再打开对应功能。' for action in actions}
        reasons = {}
        if not self.videos:
            for action in ('quality', 'naming', 'notices'):
                reasons[action] = '先在主界面粘贴视频网址并点击“查找视频”，找到结果后即可使用。'
        if not any(state == '失败' for state in self.states.values()):
            reasons['failure'] = '目前没有下载失败项。若查找时就失败，请查看弹窗或“查找情况”，调整后重新查找；下载失败后可选中对应视频查看原因。'
        if self.storage_worker:
            reasons['partials'] = '正在读取占用，请等待检查结束后再查看素材。'
            reasons['engine'] = '正在检查保存位置，请等待检查结束后再管理解析引擎。'
        return reasons

    def health_blocked(self):
        return any(item.key == 'ffmpeg' and item.state == 'error' for item in self.health_results)

    def begin_health_check(self):
        if self.health_worker:
            return
        folder = self.folder.text()
        self.health_worker = LocalJob(environment_checks, folder, preferences.PREFERENCES,
                                     self.history.path, self.queue_store.path)
        self.health_worker.checked_folder = str(folder)
        self.system_button.setText('检查与数据 · 检查中')
        self.health_worker.finished.connect(self.health_finished)
        self.health_worker.start()

    def accept_health_results(self, results, checked_folder=None):
        self.health_results = results or []
        if checked_folder is not None and checked_folder != self.folder.text():
            from .health import Check
            self.health_results = [Check('folder', '当前保存位置', 'info', '位置已更改，请重新检查',
                                         '这次结果对应之前的保存位置。请在当前目录下重新检查；下载前仍会检查实际目录。', 'folder')
                                   if item.key == 'folder' else item for item in self.health_results]
        count = sum(item.state in ('warning', 'error') for item in self.health_results)
        self.system_button.setText('检查与数据' + (f' · {count} 项' if count else ' · 正常'))
        self.log.appendPlainText('本机环境检查完成：' + str(count) + ' 项需要查看。检查不代表视频网站可用性已验证。')
        if self.health_blocked() and not self.worker:
            self.set_phase('视频组件需处理', 'error')
            self.task_label.setText('视频处理组件无法运行，请打开右上角“检查与数据”查看处理步骤。')
            self.progress_label.setText('修复后点“重新检查”。现有视频和记录不会因此自动清除。')
        elif not self.health_blocked() and self.phase_badge.text() == '视频组件需处理':
            self.set_phase('可以下载了' if self.videos else '准备就绪', 'success')
            self.task_label.setText('视频组件检查通过，可以继续查找或下载。')
        self.update_actions()
        if self.system_dialog and not self.system_dialog.job:
            self.system_dialog.show_checks()

    def health_finished(self):
        job = self.health_worker
        self.health_worker = None
        if job.error:
            self.system_button.setText('检查与数据 · 未完成')
            self.log.appendPlainText(job.error)
        else:
            self.accept_health_results(job.result, job.checked_folder)
        job.deleteLater()
        if self.close_pending:
            self.close()

    def show_system(self, checked=False, *, tab=0):
        if self.worker or self.storage_worker:
            return
        dialog = SystemDialog(self, tab=tab)
        self.system_dialog = dialog
        try:
            run_dialog(dialog)
            route = dialog.route
        finally:
            self.system_dialog = None
        if self.close_pending:
            self.close()
        elif route:
            if route == 'outputs':
                QTimer.singleShot(0, self, self.open_folder)
            else:
                QTimer.singleShot(0, self, lambda: self.run_guide_action(route))

    def show_quick_help(self, checked=False, *, topic='first'):
        dialog = UsageGuide(self, topic=topic, availability=self.guide_availability)
        if self.worker:
            self.worker.finished.connect(dialog.display_topic)
        if self.storage_worker:
            self.storage_worker.finished.connect(dialog.display_topic)
        run_dialog(dialog)
        if dialog.action:
            # Execute only after the old modal window is closed. Lifetime is
            # tied to this window; closing the app cancels the deferred action.
            QTimer.singleShot(0, self, lambda: self.run_guide_action(dialog.action))

    def run_guide_action(self, action):
        if self.worker or self.close_pending:
            return
        if action == 'source':
            self.centralWidget().ensureWidgetVisible(self.url)
            self.url.setFocus()
        elif action == 'batch':
            self.show_batch_input()
        elif action == 'quality' and self.videos:
            self.table.setCurrentCell(0, 3)
            self.table.cellWidget(0, 3).setFocus()
        elif action == 'storage':
            self.centralWidget().ensureWidgetVisible(self.folder)
            self.folder.setFocus()
            self.folder.selectAll()
        elif action == 'controls':
            self.centralWidget().ensureWidgetVisible(self.keep_progress)
            self.keep_progress.setFocus()
        elif action in ('login', 'proxy'):
            self.network_toggle.setChecked(True)
            if action == 'login':
                self.login_tabs.setCurrentIndex(1)
            else:
                self.proxy_toggle.setChecked(True)
                self.proxy.setFocus()
        elif action == 'naming' and self.videos:
            self.show_naming()
        elif action == 'failure' and any(state == '失败' for state in self.states.values()):
            self.show_failure()
        elif action == 'queue':
            self.show_queue()
        elif action == 'history':
            self.show_history()
        elif action == 'partials' and not self.storage_worker:
            self.show_progress_files()
        elif action in ('health', 'data', 'engine'):
            self.show_system(tab={'health': 0, 'data': 1, 'engine': 2}[action])
        elif action == 'notices' and self.videos:
            self.show_result_notices()

    def selected_rows(self):
        return [row for row in range(self.table.rowCount()) if self.table.item(row, 0)
                and self.table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def source_changed(self, *_):
        self.refresh_source_hint()
        self.update_actions()
        self.schedule_queue_save()

    def refresh_source_hint(self):
        stale = bool(self.videos) and self.url.text().strip() != self.results_url
        if stale:
            text = "链接已更改，下方是上次结果"
        elif self.batch_urls:
            text = f"批量结果 · {len(self.batch_urls)} 个链接"
        elif not self.use_login.isChecked() and (self.cookies.text().strip() or self.browser_session):
            text = "公开访问 · 登录信息暂不使用"
        elif self.cookies.text().strip():
            text = "已选登录文件 · 按网站匹配"
        elif self.browser_session and self.browser_session.supports(self.url.text()):
            text = ("登录信息可能已过期" if not any(not c.is_expired() for c in self.browser_session.cookies)
                    else "已添加此网站的登录信息")
        else:
            text = "公开链接可直接查找"
        self.source_hint.setText(text)
        self.source_hint.setToolTip("链接改动后，请先重新查找视频。" if stale else self.cookie_status.text())

    def paste_url(self):
        text = QApplication.clipboard().text().strip()
        if not text:
            self.source_hint.setText("剪贴板里没有文字，请先复制视频网页的网址。")
            return
        try:
            urls = shared_urls(text)
        except ValueError as exc:
            QMessageBox.information(self, "没有取得完整链接", str(exc))
            return
        if len(urls) > 1:
            self.show_batch_input("\n".join(urls))
            return
        self.url.setText(urls[0])
        self.task_label.setText("已取得视频链接，点击“查找视频”继续。")
        self.url.setFocus()

    def show_batch_input(self, text=None):
        if self.worker:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("批量查找视频")
        dialog.resize(700, 440)
        layout = QVBoxLayout(dialog)
        layout.addWidget(self.muted(f"可粘贴多个网址或分享文字，最多 {MAX_LINKS} 个链接；重复项自动合并。"))
        editor = QPlainTextEdit()
        editor.setAccessibleName("批量视频链接")
        editor.setPlaceholderText("https://example.com/video/1\nhttps://example.com/video/2")
        preset = text if isinstance(text, str) else "\n".join(self.failed_urls or self.batch_urls or [self.url.text()])
        editor.setPlainText(preset)
        editor.setMinimumHeight(200)
        layout.addWidget(editor, 1)
        layout.addWidget(self.muted("依次查找并合并结果，某个链接失败会继续下一个。最多保留 50 个视频；成功查找后替换当前列表。\n登录信息仅用于对应网站，普通链接无需额外设置。全部失败或取消时保留上次列表。"))
        status = self.muted("")
        layout.addWidget(status)
        buttons = QDialogButtonBox()
        buttons.addButton("查找这些链接", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        def submit():
            try:
                urls = shared_urls(editor.toPlainText())
                network = self.network_settings()
            except ValueError as exc:
                status.setText(str(exc))
                return
            dialog.accept()
            self.begin_batch_analysis(urls, network)
        buttons.accepted.connect(submit)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        run_dialog(dialog)

    def begin_batch_analysis(self, urls, network):
        if self.health_blocked():
            QMessageBox.information(self, '请先处理视频组件问题', '视频处理组件无法运行，请先在“检查与数据”修复并重新检查，再查找视频。')
            return
        if self.worker:
            return
        self.url.setText(urls[0])
        self.count_label.setText("2 · 正在批量查找")
        self.task_label.setText(f"准备查找 {len(urls)} 个链接…")
        self.progress.setRange(0, 0)
        self.progress_label.setText("逐个查找后合并结果；可以取消，之前的列表会保留。")
        self.start_worker("analyze_batch", urls, network)

    def show_lookup_report(self):
        paragraphs = ["批量查找情况", "点击“批量链接…”会优先填入失败或尚未处理的链接，方便再次查找。"]
        for failure in self.batch_failures:
            paragraphs.append(f"第 {failure['index']} 个链接：{redact_detail(failure['url'])}\n{failure['hint']}")
        if self.batch_pending:
            paragraphs.append(f"已达到 50 个视频的上限，另有 {len(self.batch_pending)} 个链接尚未处理，请分批查找。")
        dialog = HelpDialog("批量查找情况", "", self)
        dialog.content.setPlainText("\n\n".join(paragraphs))
        run_dialog(dialog)

    def apply_batch_quality(self):
        cap = self.batch_quality.currentData()
        if cap == "keep" or self.populating or self.worker:
            return
        changed = skipped = 0
        for row in self.selected_rows():
            combo = self.table.cellWidget(row, 3)
            if cap is None:
                combo.setCurrentIndex(0)
            else:
                available = [height for height in self.videos[row].heights if height <= cap]
                if not available:
                    skipped += 1
                    continue
                combo.setCurrentIndex(combo.findData(max(available)))
            changed += 1
        self.task_label.setText(f"已为 {changed} 个视频设置画质。" + (f"另有 {skipped} 个没有对应画质，保留原选择。" if skipped else ""))
        self.batch_quality.blockSignals(True)
        self.batch_quality.setCurrentIndex(0)
        self.batch_quality.blockSignals(False)

    def update_actions(self):
        busy = self.worker is not None
        selected = len(self.selected_rows())
        current = not self.videos or self.url.text().strip() == self.results_url
        self.analyze_button.setEnabled(not busy and bool(self.url.text().strip()))
        self.paste_button.setEnabled(not busy)
        self.batch_button.setEnabled(not busy)
        self.rate_limit.setEnabled(not busy)
        self.keep_progress.setEnabled(not busy)
        self.progress_files_button.setEnabled(not busy and not self.storage_worker)
        self.system_button.setEnabled(not busy and not self.storage_worker)
        self.queue_button.setEnabled(not busy)
        self.url.setEnabled(not busy)
        self.table.setEnabled(not busy)
        self.select_all.setEnabled(not busy and bool(self.videos))
        self.folder.setEnabled(not busy)
        self.browse_button.setEnabled(not busy)
        self.network_panel.setEnabled(not busy)
        self.network_toggle.setEnabled(not busy)
        self.download_button.setEnabled(not busy and current and selected > 0)
        if self.storage_worker:
            self.download_button.setEnabled(False)
        self.download_button.setText(f"下载选中视频（{selected}）" if selected else "先勾选要下载的视频")
        retry_count = sum(s in ("失败", "已取消", "已跳过") for s in self.states.values())
        self.retry_button.setText(f"重试未完成（{retry_count}）" if retry_count else "重试未完成")
        self.retry_button.setEnabled(not busy and current and retry_count > 0)
        if self.storage_worker:
            self.retry_button.setEnabled(False)
        self.storage_refresh.setEnabled(not busy and not self.storage_worker)
        self.cancel_button.setEnabled(busy and not self.worker.cancel.is_set())
        self.cancel_button.setText("取消全部" if busy and self.worker.operation == "download" else "取消任务")
        self.error_button.setEnabled(not busy and any(s == "失败" for s in self.states.values()))
        self.clear_cookies.setEnabled(not busy and (self.browser_session is not None or bool(self.cookies.text().strip())))
        self.cookie_check.setEnabled(not busy and (self.browser_session is not None or bool(self.cookies.text().strip())))
        self.use_login.setEnabled(not busy and (self.browser_session is not None or bool(self.cookies.text().strip())))
        self.settings_lookup.setEnabled(not busy and bool(self.url.text().strip()))
        self.history_button.setEnabled(not busy)
        self.lookup_report_button.setEnabled(not busy)
        self.batch_quality.setEnabled(not busy and current and selected > 0)
        self.naming_button.setEnabled(not busy and bool(self.videos))
        self.skip_button.setEnabled(bool(busy and self.worker.can_skip(self.current_row)))
        done = sum(state == "已完成" for state in self.states.values())
        failed = sum(state == "失败" for state in self.states.values())
        self.selection_label.setText(f"已选 {selected} · 已完成 {done}" + (f" · 失败 {failed}" if failed else "") if self.videos else "")
        if self.health_blocked():
            self.analyze_button.setEnabled(False)
            self.batch_button.setEnabled(False)
            self.download_button.setEnabled(False)
            self.retry_button.setEnabled(False)

    def network_settings(self):
        proxy = self.proxy.text().strip()
        if proxy:
            proxy = validate_url(proxy)
        cookies = self.cookies.text().strip() if self.use_login.isChecked() else ""
        if cookies and not Path(cookies).is_file():
            raise ValueError("Cookies 文件不存在，请重新选择；若要尝试公开视频，可在“登录与代理”中取消勾选“使用已添加的登录信息”。原设置会保留。")
        if cookies:
            try:
                status = Path(cookies).stat()
                key = (str(Path(cookies).resolve()), status.st_size, status.st_mtime_ns)
                if key != self.cookie_validation_key:
                    load_cookie_file(cookies)
                    self.cookie_validation_key = key
            except ValueError as exc:
                raise ValueError(str(exc) + "\n若要尝试公开视频，可暂时取消勾选“使用已添加的登录信息”；原设置会保留。") from None
            except OSError:
                raise ValueError("登录文件暂时无法读取，请检查文件位置和访问权限。") from None
        return Network(proxy, cookies, self.rate_limit.currentData(), self.keep_progress.isChecked())

    def browser_changed(self):
        self.browser_profile.clear()
        self.refresh_profiles()

    def refresh_profiles(self):
        browser = self.cookie_browser.currentData()
        profiles = discover_profiles(browser)
        self.profile_picker.blockSignals(True)
        self.profile_picker.clear()
        self.profile_picker.addItem("请选择你实际登录过的浏览器用户" if len(profiles) > 1 else "自动查找，或手动指定资料位置", "")
        for profile in profiles:
            self.profile_picker.addItem(profile.name, str(profile))
        self.profile_picker.blockSignals(False)
        if len(profiles) == 1 and not self.browser_profile.text().strip():
            self.profile_picker.setCurrentIndex(1)
        self.browser_notice.setText("读取可能受占用或加密限制；失败时可切换“文件导入”。"
                                    if browser in ("edge", "chrome", "brave") else "选择实际登录过网站的用户；通常无需手动填写路径。")
        if not profiles:
            self.browser_notice.setText(self.browser_notice.text() + " 未检测到默认位置的用户资料；便携版可手动指定文件夹。")

    def choose_detected_profile(self):
        self.browser_profile.setText(self.profile_picker.currentData() or "")

    def browse_profile(self):
        root = browser_root(self.cookie_browser.currentData())
        folder = QFileDialog.getExistingDirectory(self, "选择已登录浏览器的用户资料文件夹", str(root if root.is_dir() else Path.home()))
        if folder:
            self.browser_profile.setText(folder)

    def start_worker(self, operation, payload, network):
        self.size_timer.stop()
        session = self.browser_session if self.use_login.isChecked() else None
        if operation == "cookies" or network.cookie_file:
            session = None
        elif operation == "analyze" and not (session and session.supports(payload)):
            session = None
        naming = ({row: {"filename": self.custom_names.get(row, ""), "naming": self.naming_mode}
                   for row, _video, _height in payload[0]} if operation == "download" else None)
        self.worker = Worker(operation, payload, network, session, naming=naming,
                             history=self.history if operation == 'download' else None)
        self.worker.event.connect(self.handle_event)
        self.worker.finished.connect(self.worker_finished)
        self.set_phase({"analyze": "查找中", "analyze_batch": "批量查找中", "download": "下载中", "cookies": "读取登录状态", "restore_queue": "恢复队列中"}[operation])
        self.update_actions()
        self.worker.start()

    def start_cookie_read(self):
        if self.worker:
            return
        try:
            url = validate_url(self.url.text())
        except ValueError as exc:
            QMessageBox.information(self, "先填写视频网页网址", str(exc))
            return
        if not self.browser_profile.text().strip() and self.profile_picker.count() > 2:
            QMessageBox.information(self.network_panel, "先选浏览器用户", "检测到多个用户资料。请在下拉框里选择你实际登录过网站的那个用户，再读取。")
            return
        self.network_panel.hide()
        self.network_toggle.setChecked(False)
        self.progress.setRange(0, 0)
        self.task_label.setText("正在读取浏览器中这个网站的登录状态…")
        self.progress_label.setText("可能需要几秒钟；不会打开网站，也不会修改浏览器资料。")
        self.start_worker("cookies", (self.cookie_browser.currentData(), self.browser_profile.text(), url), Network())

    def clear_browser_session(self, *_):
        self.browser_session = None
        self.import_summary = ""
        self.import_site = None
        self.update_cookie_status()
        self.update_actions()

    def clear_login_information(self):
        self.browser_session = None
        self.cookies.clear()
        self.import_summary = ""
        self.import_site = None
        self.update_cookie_status()
        self.update_actions()

    def update_cookie_status(self, *_):
        session = self.browser_session
        if not self.use_login.isChecked() and (self.cookies.text().strip() or session):
            self.cookie_status.setText("当前按公开访问尝试，不发送已添加的 Cookies。原来的登录设置仍保留，重新勾选即可恢复。")
            self.refresh_source_hint()
            return
        if session is None:
            try:
                matching_site = self.import_site == site_scope(self.url.text())
            except ValueError:
                matching_site = False
            text = (((self.import_summary if matching_site else "") or "已选择登录文件。每个链接只用对应网站的记录，没有匹配时先尝试公开访问。想取消使用，可点“清除登录信息”。") if self.cookies.text().strip()
                    else "尚未使用登录信息。普通视频可以直接查找。")
            self.cookie_status.setText(text)
        elif not session.supports(self.url.text()):
            self.cookie_status.setText("当前链接属于另一个网站，之前读取的登录信息不会带过去。")
        else:
            if not any(not cookie.is_expired() for cookie in session.cookies):
                self.cookie_status.setText("之前读取的登录信息已过期。普通视频可继续查找，需要登录时请重新读取。")
                self.refresh_source_hint()
                return
            note = "；部分内容无法读取，登录信息可能不完整" if session.partial else ""
            if session.login_marker is False:
                note += "；未找到常见登录标记，请确认浏览器已登录，或用导出文件方式重试"
            origin = "已从剪贴板导入" if session.browser == "clipboard" else "已读取"
            self.cookie_status.setText(f"{origin} {', '.join(session.domains)} 的登录信息（{len(session.cookies)} 条）{note}。关闭软件后清除。")
        self.refresh_source_hint()

    def start_analysis(self):
        if self.health_blocked():
            QMessageBox.information(self, '请先处理视频组件问题', '视频处理组件无法运行，请在“检查与数据”中查看说明，修复后重新检查。')
            return
        if self.worker:
            return
        try:
            urls = shared_urls(self.url.text())
            if len(urls) > 1:
                self.show_batch_input("\n".join(urls))
                return
            url = urls[0]
            network = self.network_settings()
        except ValueError as exc:
            QMessageBox.information(self, "检查网址", str(exc))
            return
        self.url.setText(url)
        self.count_label.setText("2 · 正在查找视频")
        self.task_label.setText("正在查找网页中的视频…")
        self.progress.setRange(0, 0)
        self.progress_label.setText("网站响应较慢时需要多等一会儿；可以点击“取消任务”。")
        self.start_worker("analyze", url, network)

    def populate(self, videos):
        self.populating = True
        self.queue_unresolved = []
        self.queue_issues = []
        self.custom_names = {}
        self.size_cache = {}
        self.videos = videos
        self.states.clear()
        self.paths.clear()
        self.results_url = self.url.text().strip()
        self.table.setRowCount(len(videos))
        self.select_all.blockSignals(True)
        self.select_all.setChecked(True)
        self.select_all.blockSignals(False)
        for row, video in enumerate(videos):
            select = QTableWidgetItem()
            select.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            select.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, select)
            title = QTableWidgetItem(video.title)
            title.setToolTip(video.title + ("\n来源：" + redact_detail(video.referer) if video.referer else ""))
            self.table.setItem(row, 1, title)
            self.table.setItem(row, 2, QTableWidgetItem(duration_label(video.duration)))
            combo = QualityCombo(lambda row=row: self.prepare_quality_menu(row))
            combo.addItem("最高画质" if video.heights else "原画质", None)
            for height in video.heights:
                combo.addItem(f"{height}p", height)
            combo.setToolTip("例如选择 720p，会下载不超过 720p 的最佳画质；不会放大低清视频。")
            self.table.setCellWidget(row, 3, combo)
            combo.currentIndexChanged.connect(self.schedule_queue_save)
            combo.currentIndexChanged.connect(self.schedule_size_hints)
            self.table.setItem(row, 4, QTableWidgetItem("待下载"))
            self.table.setItem(row, 5, QTableWidgetItem())
            self.update_result_notice(row)
            self.states[row] = "待下载"
            self.table.setRowHeight(row, 46)
        self.batch_quality.blockSignals(True)
        self.batch_quality.clear()
        self.batch_quality.addItem("批量设置画质", "keep")
        self.batch_quality.addItem("最高画质", None)
        for height in sorted({height for video in videos for height in video.heights}, reverse=True):
            self.batch_quality.addItem(f"不超过 {height}p", height)
        self.batch_quality.blockSignals(False)
        self.populating = False
        self.results_stack.setCurrentIndex(1 if videos else 0)
        self.count_label.setText(f"2 · 选择视频（{len(videos)}）")
        self.empty_hint.setText("勾选需要的视频，再点下方下载。画质可逐行选择。")
        warned = sum(any(note.warning for note in video.notices) for video in videos)
        collection = any(video.info.get('_shiying_collection_notice') for video in videos)
        if warned:
            self.empty_hint.setText(f'{warned} 个视频有内容或画质提示。点击“解析提示…”查看' + ('；所属网页 / 列表也有查找情况说明。' if collection else '；下载成功不代表源内容完整。'))
        elif collection:
            self.empty_hint.setText('所属网页 / 列表有查找情况说明，可点击“解析提示…”查看；不代表当前视频损坏。')
        self.notice_button.setEnabled(bool(videos))
        self.source_changed()
        self.schedule_queue_save()
        self.schedule_size_hints()

    def update_result_notice(self, row):
        if not 0 <= row < len(self.videos):
            return
        video = self.videos[row]
        item = self.table.item(row, 5)
        if item is None:
            return
        item.setText(notice_brief(video.info))
        item.setToolTip('双击查看详细说明。\n' + '\n'.join(note.title + '：' + note.detail for note in video.notices))
        item.setForeground(QColor('#946200' if any(note.warning for note in video.notices) else '#6b7890'))

    def show_result_notices(self, row=None):
        if not self.videos:
            return
        if row is None:
            row = max(0, self.table.currentRow())
        run_dialog(ResultNoticeDialog(self.videos, row, self))

    def schedule_size_hints(self, *_):
        if not self.populating:
            self.size_timer.start(100)

    def cache_sizes(self, requests):
        missing = [(row, video, quality) for row, video, quality in requests if (id(video.info), quality) not in self.size_cache]
        if not missing:
            return
        index = {row: (video, quality) for row, video, quality in missing}
        for row, size in estimate_sizes(missing).items():
            video, quality = index[row]
            self.size_cache[(id(video.info), quality)] = size

    def label_quality_size(self, combo, index, video):
        quality = combo.itemData(index)
        key = (id(video.info), quality)
        if key not in self.size_cache:
            return
        size = self.size_cache[key]
        label = f"{quality}p" if quality else "最高" if video.heights else "原画质"
        description = f"约 {bytes_label(size)}" if size is not None else "大小未知"
        combo.setItemText(index, f"{label} · {description}")
        combo.setItemData(index, f"{label}：{description}。按网站提供的素材大小或码率估算，包含单独音轨；MP4 整理后可能变化。", Qt.ItemDataRole.ToolTipRole)

    def prepare_quality_menu(self, row):
        if self.worker or row >= len(self.videos):
            return
        video, combo = self.videos[row], self.table.cellWidget(row, 3)
        requests = [(index, video, combo.itemData(index)) for index in range(combo.count())]
        self.cache_sizes(requests)
        for index in range(combo.count()):
            self.label_quality_size(combo, index, video)

    def refresh_size_hints(self):
        if self.worker or not self.videos or self.url.text().strip() != self.results_url:
            return
        requests = [(row, video, self.table.cellWidget(row, 3).currentData()) for row, video in enumerate(self.videos)]
        self.cache_sizes(requests)
        for row, video, _quality in requests:
            combo = self.table.cellWidget(row, 3)
            self.label_quality_size(combo, combo.currentIndex(), video)
        selected = [self.size_cache[(id(self.videos[row].info), self.table.cellWidget(row, 3).currentData())] for row in self.selected_rows()]
        known = [size for size in selected if size is not None]
        unknown = len(selected) - len(known)
        if selected:
            text = f"选中素材约 {bytes_label(sum(known))}" if known else "选中视频的大小暂时未知"
            if unknown and known:
                text += f"，另有 {unknown} 项大小未知"
            self.progress_label.setText(text + "；MP4 整理还需额外空间，完成后再播放。")
        else:
            self.progress_label.setText("勾选需要的视频，即可查看预计下载量。")

    def check_download_space(self, items, folder, *, sizes=None):
        estimate = estimate_space(items, sizes=sizes)
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            self.log.appendPlainText("未能读取保存盘的剩余空间，请自行确认；本次仍可尝试下载。")
            return True
        note = f"另有 {estimate.unknown} 个视频暂时不知道大小。" if estimate.unknown else ""
        volume = f"已知下载量约 {bytes_label(estimate.source_bytes)}" if estimate.source_bytes else "暂时无法估算下载量"
        self.log.appendPlainText(f"保存盘可用 {bytes_label(free)}；{volume}。{note}合并、转码还需要额外空间。")
        if free >= estimate.recommended_bytes:
            return True
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("保存盘空间可能不足")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(f"当前可用空间：{bytes_label(free)}\n建议至少预留：{bytes_label(estimate.recommended_bytes)}")
        dialog.setInformativeText(note + ("未知大小的部分未计入，实际需要更多空间。" if estimate.unknown else "") + "这是根据网站提供的大小或码率估算，包含合并和整理文件的预留空间，实际可能更多。建议更换保存位置或减少勾选项。即使选择继续，下载期间的空间保护仍然有效；剩余空间低于 64 MB 或不足以整理当前视频时仍会停止。")
        back = dialog.addButton("返回调整", QMessageBox.ButtonRole.RejectRole)
        proceed = dialog.addButton("仍然继续", QMessageBox.ButtonRole.AcceptRole)
        dialog.setDefaultButton(back)
        dialog.setEscapeButton(back)
        run_dialog(dialog)
        return dialog.clickedButton() == proceed

    def toggle_all(self, checked):
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self.sync_selection()

    def sync_selection(self, item=None):
        if self.populating or (item is not None and item.column() != 0):
            return
        all_checked = self.table.rowCount() > 0 and all(
            self.table.item(row, 0) is not None and self.table.item(row, 0).checkState() == Qt.CheckState.Checked
            for row in range(self.table.rowCount()))
        self.select_all.blockSignals(True)
        self.select_all.setChecked(all_checked)
        self.select_all.blockSignals(False)
        self.update_actions()
        self.schedule_queue_save()
        self.schedule_size_hints()

    def start_download(self, checked=False, retry_only=False, only_rows=None):
        if self.health_blocked():
            QMessageBox.information(self, '请先处理视频组件问题', '启动检查发现 MP4 处理组件无法运行。请打开“检查与数据”，按说明修复后重新检查。')
            return
        if self.worker or self.storage_worker or not self.videos:
            return
        if self.url.text().strip() != self.results_url:
            QMessageBox.information(self, "请先查找新链接", "链接已更改，请先点击“查找视频”，再下载新结果。")
            return
        items = []
        for row, video in enumerate(self.videos):
            if only_rows is not None and row not in only_rows:
                continue
            if retry_only:
                include = self.states.get(row) in ("失败", "已取消", "已跳过")
            else:
                include = self.table.item(row, 0).checkState() == Qt.CheckState.Checked
            if include:
                items.append((row, video, self.table.cellWidget(row, 3).currentData()))
        if not items:
            QMessageBox.information(self, "选择视频", "请至少勾选一个视频。")
            return
        items = self.check_previous_downloads(items)
        if not items:
            return
        text = self.folder.text().strip()
        if not text:
            QMessageBox.information(self, "选择保存位置", "请选择一个用于保存 MP4 的文件夹。")
            return
        try:
            network = self.network_settings()
        except ValueError as exc:
            QMessageBox.information(self, "请检查登录与代理设置", str(exc))
            return
        try:
            folder = Path(text).expanduser().resolve()
            self.cache_sizes(items)
            sizes = {row: self.size_cache[(id(video.info), height)] for row, video, height in items}
            stems = [filename_stem(video.title, height, filename=self.custom_names.get(row, ''), naming=self.naming_mode)
                     for row, video, height in items]
            preflight_destination(folder, estimated_bytes=max((size for size in sizes.values() if size), default=0), stems=stems)
        except (OSError, ValueError, RuntimeError) as exc:
            hint = explain_error(exc)
            QMessageBox.warning(self, '保存位置需要调整', hint.as_text())
            return
        if not self.check_download_space(items, folder, sizes=sizes):
            return
        self.folder.setText(str(folder))
        self.remember_folder(folder)
        for row, _, _ in items:
            self.states[row] = "排队中"
            self.table.item(row, 4).setText("排队中")
            self.table.item(row, 4).setToolTip("")
            self.table.item(row, 4).setData(Qt.ItemDataRole.UserRole, "")
            self.table.item(row, 4).setData(Qt.ItemDataRole.UserRole + 1, None)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.task_label.setText(f"准备下载 {len(items)} 个视频…")
        self.start_worker("download", (items, folder), network)

    def handle_event(self, kind, data):
        if kind == 'storage':
            self.storage_hint.setText(f"保存盘可用 {bytes_label(data['free'])} · 最近检查值；未完成素材占用可在下载结束后刷新。")
        elif kind == "log":
            self.log.appendPlainText(redact_detail(data["text"]))
        elif kind == "lookup":
            self.task_label.setText(f"正在查找第 {data['index']} / {data['total']} 个链接…")
            self.progress_label.setText("正在读取网页与画质信息，失败链接会记录下来，不影响后续链接。")
        elif kind == "current":
            self.current_row = data["row"]
            self.task_label.setText(data["text"])
            self.task_label.setToolTip(data["text"])
            self.progress.setValue(0)
            self.table.scrollToItem(self.table.item(self.current_row, 1))
        elif kind == "row":
            row = data["row"]
            self.update_result_notice(row)
            self.states[row] = data["state"]
            item = self.table.item(row, 4)
            item.setText(data["state"])
            item.setToolTip(data.get("error") or data.get("path") or "")
            item.setData(Qt.ItemDataRole.UserRole, data.get("detail", ""))
            item.setData(Qt.ItemDataRole.UserRole + 1, data.get("hint"))
            item.setForeground(QColor({"已完成": "#23734f", "失败": "#b24c32", "下载中": "#3464dc"}.get(data["state"], "#6b7890")))
            if data.get("path"):
                self.paths[row] = data["path"]
                self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
                if not data.get("existing") and not data.get('history_handled'):
                    try:
                        self.history.record(self.videos[row], self.table.cellWidget(row, 3).currentData(), data["path"])
                    except (OSError, sqlite3.Error):
                        self.log.appendPlainText("视频已保存，但下载历史暂时写入失败；不影响视频文件。")
            self.update_actions()
            self.schedule_queue_save()
        elif kind == "progress":
            value = data.get("percent")
            if value is None:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 100)
                self.progress.setValue(int(value))
            phase = {"转为 MP4": "整理为 MP4", "封装为 MP4": "保存为 MP4", "转码为 MP4": "转换为 MP4，请稍候",
                     "正在合并音视频": "合并画面与声音", "下载完成，准备合并": "下载完成，准备整理文件",
                     '记录保存结果': '视频已保存，正在更新历史'}.get(data["phase"], data["phase"])
            details = ["当前视频", phase]
            if data["phase"] == "等待重试":
                seconds = data.get("retry_seconds", 0)
                details = [data.get("retry_reason", "连接暂时中断"),
                           f"{seconds} 秒后重试" if seconds else "正在重新连接",
                           f"第 {data.get('retry_attempt', 1)} 次重试", "可以取消"]
            if value is not None:
                details.append(f"{value:.0f}%")
            if data["phase"] == "下载":
                if data.get("resumed"):
                    details.append("接着下载")
                details.append(speed_label(data.get("speed")))
                if data.get("eta") is not None:
                    details.append(f"本次传输剩余约 {duration_label(data['eta'])}")
            self.progress_label.setText("  ·  ".join(details))
            if self.current_row is not None and self.states.get(self.current_row) == "下载中":
                text = ((f"下载 {value:.0f}%" if value is not None else "下载中") if data["phase"] == "下载"
                        else (f"等待重试 {data['retry_seconds']}秒" if data.get("retry_seconds") else "重新连接")
                        if data["phase"] == "等待重试"
                        else "核对进度" if data["phase"] == "核对下载进度" else "整理文件")
                self.table.item(self.current_row, 4).setText(text)

    def worker_finished(self):
        worker = self.worker
        if worker.operation == "download":
            # Row completion unchecks items and schedules an idle size update.
            # Don't let that pending timer replace the final success/error hint.
            self.size_timer.stop()
        self.worker = None
        self.progress.setRange(0, 100)
        if worker.error:
            self.set_phase("未完成", "error")
            self.progress.setValue(0)
            self.task_label.setText("这次操作未完成。请按错误提示检查后重试。")
            self.progress_label.setText("可以展开“运行记录”查看详情；已经保存的视频不受影响。")
            self.log.appendPlainText(worker.error_hint.as_text(include_detail=True) if worker.error_hint else worker.error)
            if worker.operation == "download":
                for row, state in list(self.states.items()):
                    if state in ("排队中", "下载中"):
                        self.handle_event("row", {"row": row, "state": "失败", "error": worker.error,
                                                   "detail": worker.error_hint.detail if worker.error_hint else "", "hint": worker.error_hint})
            if worker.operation in ("analyze", "analyze_batch"):
                self.count_label.setText("2 · 上次查找结果" if self.videos else "2 · 选择视频")
                self.blank_hint.setText("这次没有找到视频。可以检查链接、网络，或在“登录与代理”中添加登录信息后再试。")
            if not self.close_pending:
                self.error_context = {"hint": worker.error_hint, "operation": worker.operation,
                                      "url": worker.payload if worker.operation == "analyze" else self.url.text(),
                                      "row": None}
                self.error_dialog(worker.error_hint.title if worker.error_hint else "任务未完成", worker.error,
                                  worker.error_hint.detail if worker.error_hint else "")
            if worker.operation == "cookies":
                self.progress_label.setText("之前的登录设置仍保留。可在“登录与代理”中改用文件或复制导入。")
        elif worker.cancelled:
            self.set_phase("已取消")
            self.progress.setValue(0)
            self.task_label.setText("任务已取消。已完成的 MP4 文件已保留。")
            self.progress_label.setText("可点击“重试未完成”；勾选保留进度时会核对能否续传，已完成的视频不会重下。")
            if worker.operation in ("analyze", "analyze_batch"):
                self.count_label.setText("2 · 上次查找结果" if self.videos else "2 · 选择视频")
                self.task_label.setText("查找已取消。" + ("上次的视频列表仍然保留。" if self.videos else ""))
                self.progress_label.setText("可以修改链接后重新查找，已保存的文件不受影响。")
            elif worker.operation == "cookies":
                self.task_label.setText("已取消读取登录信息。")
                self.progress_label.setText("之前的登录设置仍保留；普通视频也可直接查找。")
            elif worker.operation == "restore_queue":
                self.task_label.setText("队列恢复已取消。已记住的任务和上次列表保留。")
                self.progress_label.setText("点击“任务队列”可再次重新查找恢复，不会自动下载。")
            for row, state in self.states.items():
                if state in ("排队中", "下载中"):
                    self.states[row] = "已取消"
                    self.table.item(row, 4).setText("已取消")
        elif worker.operation == "cookies":
            self.browser_session = worker.result
            worker.result = None
            self.cookies.clear()
            self.use_login.setChecked(True)
            self.update_cookie_status()
            self.progress.setValue(0)
            marker = self.browser_session.login_marker
            self.set_phase("未确认已登录" if marker is False else "Cookies 已读取", "" if marker is False else "success")
            self.task_label.setText("读到了网站 Cookies，但未找到常见登录标记。请确认浏览器已登录，或改用文件导入。" if marker is False
                                    else "已读取这个网站的 Cookies，现在可以重新查找视频；账号权限仍需网站确认。")
            self.progress_label.setText("如果仍无法访问，请确认这个浏览器中的账号能正常播放该视频。")
            self.log.appendPlainText(self.cookie_status.text())
        elif worker.operation == "restore_queue":
            result = worker.result
            self.queue_issues = result["issues"]
            if result["videos"]:
                self.batch_urls = list(dict.fromkeys(video.referer for video in result["videos"]))
                self.failed_urls = []
                self.batch_failures = []
                self.batch_pending = []
                self.lookup_report_button.hide()
                self.populate(result["videos"])
                self.queue_issues = result["issues"]
                self.naming_mode = result.get("naming_mode", "title_quality")
                self.custom_names = {row: name for row, name in enumerate(result.get("filenames", [])) if name}
                self.populating = True
                for row, quality in enumerate(result["qualities"]):
                    self.table.cellWidget(row, 3).setCurrentIndex(self.table.cellWidget(row, 3).findData(quality))
                self.populating = False
                self.queue_unresolved = result["pending"]
                self.folder.setText(result["folder"] or self.folder.text())
                self.persist_queue()
                self.set_phase("队列已恢复", "success")
                self.task_label.setText(f"恢复了 {len(self.videos)} 项，另有 {len(result['pending'])} 项未恢复。请确认后点击下载。")
                self.progress_label.setText("未恢复的项目仍在队列中；点击“任务队列”查看原因。" if result["pending"] else "画质、文件命名和保存位置已恢复；尚未开始下载。")
            else:
                self.set_phase("队列暂未恢复", "error")
                self.task_label.setText("没有恢复出可下载项目。已记住的队列和上次列表保留，可点“任务队列”查看原因。")
            self.progress.setValue(0)
        elif worker.operation == "analyze_batch":
            result = worker.result
            self.batch_failures = result["failures"]
            self.batch_pending = result["pending"]
            self.failed_urls = [item["url"] for item in self.batch_failures] + self.batch_pending
            self.lookup_report_button.setVisible(bool(self.failed_urls))
            if result["videos"]:
                self.batch_urls = list(worker.payload)
                self.populate(result["videos"])
                self.set_phase("部分链接未完成" if self.failed_urls else "可以下载了", "" if self.failed_urls else "success")
                self.task_label.setText(f"查找了 {result['processed']} 个链接，找到 {len(self.videos)} 个视频。" + ("部分链接未完成，可点“查找情况”。" if self.failed_urls else "选好画质后即可下载。"))
                self.progress_label.setText("需要重试失败或剩余的链接时，点击“批量链接…”，会自动填入。")
            else:
                self.set_phase("未找到视频", "error")
                self.count_label.setText("2 · 上次查找结果" if self.videos else "2 · 选择视频")
                self.task_label.setText("这批链接未找到视频。上次列表已保留；点击“查找情况”查看原因。")
                self.progress_label.setText("检查链接、网络或登录信息后，可点“批量链接…”重新查找失败链接。")
            self.progress.setValue(0)
        elif worker.operation == "analyze":
            self.batch_urls = []
            self.failed_urls = []
            self.batch_failures = []
            self.batch_pending = []
            self.lookup_report_button.hide()
            self.set_phase("可以下载了", "success")
            self.populate(worker.result)
            self.progress.setValue(0)
            self.task_label.setText(f"找到 {len(self.videos)} 个视频。选好画质后，点击“下载选中视频”。")
            self.progress_label.setText("会保存为 MP4；某些视频需要额外整理格式，请等待“已完成”。")
        else:
            counts = worker.result
            unfinished = counts["failed"] + counts.get("skipped", 0)
            self.set_phase("下载完成" if not unfinished else "部分未完成", "success" if not unfinished else "error" if counts["failed"] else "")
            self.progress.setValue(100 if not unfinished else 0)
            self.task_label.setText(f"本轮下载结束 · 完成 {counts['ok']} 个，失败 {counts['failed']} 个，跳过 {counts.get('skipped', 0)} 个。")
            self.progress_label.setText("可点“重试未完成”处理失败或跳过的视频，失败项可查看原因。" if unfinished
                                        else "文件已保存。打开文件夹查看，或双击列表中已完成的视频播放。")
            if counts.get('storage_stopped'):
                self.set_phase('保存位置需要处理', 'error')
                self.task_label.setText(f"已停止后续下载 · 完成 {counts['ok']} 个，失败 {counts['failed']} 个，另有 {counts['pending']} 个尚未开始。")
                self.progress_label.setText('选中失败项查看原因。处理空间或保存位置后，点“重试未完成”；可用素材按“保留进度”设置保留。')
        self.current_row = None
        self.task_label.setToolTip("")
        worker.deleteLater()
        self.update_actions()
        if self.close_pending:
            self.close()

    def cancel_task(self):
        if self.worker:
            self.worker.cancel.set()
            self.task_label.setText("正在结束任务并保存或清理进度…")
            self.set_phase("正在取消")
            self.progress_label.setText("请稍候。勾选保留进度时，会留下符合续传条件的素材；已完成文件会保留。")
            self.update_actions()

    def skip_current(self):
        if self.worker and self.worker.request_skip(self.current_row):
            self.task_label.setText("正在跳过当前视频，随后继续队列…")
            self.progress_label.setText("当前请求结束后生效；保留进度的规则不变，已完成的文件不会撤回。")
            self.update_actions()

    def show_naming(self):
        if self.worker or not self.videos:
            return
        dialog = NamingDialog(self)
        if run_dialog(dialog) == QDialog.DialogCode.Accepted:
            self.naming_mode = dialog.mode.currentData()
            self.custom_names = dialog.names()
            self.schedule_queue_save()
            self.task_label.setText("文件命名已更新，只影响接下来的下载；已有视频不会改名。")

    def refresh_queue_button(self):
        count = len(self.queue_store.snapshot["items"])
        self.queue_button.setText(f"任务队列（{count}）" if count else "任务队列…")

    def schedule_queue_save(self, *_):
        if self.queue_store.enabled and not self.populating:
            self.queue_timer.start(400)

    def persist_queue(self):
        self.queue_timer.stop()
        if (not self.queue_store.enabled or not self.videos or self.url.text().strip() != self.results_url
                or self.worker and self.worker.operation in ("analyze", "analyze_batch", "restore_queue")):
            return True
        items = list(self.queue_unresolved)
        for row in self.selected_rows():
            if self.states.get(row) != "已完成":
                video = self.videos[row]
                if video.referer:
                    items.append({"url": video.referer, "identity": video_identity(video),
                                  "title": video.title[:300], "quality": self.table.cellWidget(row, 3).currentData()})
                    if self.custom_names.get(row):
                        items[-1]["filename"] = self.custom_names[row]
        try:
            self.queue_store.save(items, self.folder.text(), naming_mode=self.naming_mode)
            self.refresh_queue_button()
            return True
        except (OSError, ValueError):
            self.log.appendPlainText("本次未能更新记住的队列；下载不受影响，重启后可能仍显示旧记录。")
            return False

    def set_queue_memory(self, enabled):
        self.queue_timer.stop()
        previous = self.queue_store.enabled
        try:
            if enabled:
                self.queue_store.enabled = True
                if self.videos and self.url.text().strip() == self.results_url:
                    if not self.persist_queue():
                        self.queue_store.enabled = previous
                        return False
                else:
                    self.queue_store.save(self.queue_store.snapshot["items"], self.queue_store.snapshot["folder"], naming_mode=self.naming_mode)
            else:
                self.queue_store.clear()
                self.queue_unresolved = []
                self.queue_issues = []
            self.refresh_queue_button()
            return True
        except (OSError, ValueError):
            self.queue_store.enabled = previous
            return False

    def show_queue(self):
        self.persist_queue()
        run_dialog(QueueDialog(self))

    def remove_queue_items(self, indices):
        if self.worker:
            return False
        self.queue_timer.stop()
        try:
            removed = self.queue_store.remove_items(indices)
        except (OSError, ValueError):
            return False
        keys = {job_key(item) for item in removed}
        self.queue_unresolved = [item for item in self.queue_unresolved if job_key(item) not in keys]
        self.queue_issues = []
        self.populating = True
        try:
            for row, video in enumerate(self.videos):
                if not video.referer:
                    continue
                key = (validate_url(video.referer), video_identity(video), self.table.cellWidget(row, 3).currentData())
                if key in keys:
                    self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        finally:
            self.populating = False
        self.sync_selection()
        self.refresh_queue_button()
        return True

    def begin_queue_restore(self):
        if self.health_blocked():
            QMessageBox.information(self, '请先处理视频组件问题', '视频处理组件无法运行，请先在“检查与数据”修复并重新检查，再恢复任务。')
            return False
        if self.worker or not self.queue_store.snapshot["items"]:
            return False
        try:
            network = self.network_settings()
        except ValueError as exc:
            QMessageBox.information(self, "检查登录与代理设置", str(exc))
            return False
        snapshot = copy.deepcopy(self.queue_store.snapshot)
        self.url.setText(snapshot["items"][0]["url"])
        self.progress.setRange(0, 0)
        self.task_label.setText("正在重新读取原网页并核对队列中的视频…")
        self.progress_label.setText("只恢复列表，不自动下载；没有找到的项目会保留。")
        self.start_worker("restore_queue", snapshot, network)
        return True

    def error_dialog(self, title, text, detail=""):
        context = self.error_context or {"operation": "download", "url": self.url.text(), "row": None}
        self.error_context = None
        hint = context.get("hint") or explain_error(RuntimeError(detail or text), context["operation"])
        login = ("file" if self.cookies.text().strip() else "session" if self.browser_session and self.browser_session.supports(context["url"]) else "anonymous") if self.use_login.isChecked() else "anonymous"
        report = build_report(hint, context["operation"], context["url"], login=login, proxy=self.proxy.text().strip())
        dialog = DiagnosticDialog(title, text, hint, report, context["operation"], self, allow_public=login != "anonymous")
        run_dialog(dialog)
        if dialog.action:
            action = dialog.action
            # worker_finished must finish updating/deleting the old worker first.
            QTimer.singleShot(0, self, lambda: self.run_error_action(action, context))

    def run_error_action(self, action, context):
        if self.worker or self.close_pending:
            return
        if action == "public":
            self.use_login.setChecked(False)
            action = "retry"
        if action in ("import", "login", "network"):
            self.network_toggle.setChecked(True)
            if action in ("import", "login"):
                self.login_tabs.setCurrentIndex(1)
            self.network_panel.raise_()
            self.network_panel.activateWindow()
        elif action == "folder":
            self.browse()
        elif action == "lookup":
            if context["url"]:
                self.url.setText(context["url"])
                self.start_analysis()
        elif action == "retry":
            operation = context["operation"]
            if operation == "cookies":
                self.start_cookie_read()
            elif operation == "analyze":
                if self.url.text().strip() == context["url"]:
                    self.start_analysis()
            elif operation == "analyze_batch":
                self.show_batch_input()
            elif operation == "restore_queue":
                self.begin_queue_restore()
            elif operation == "download":
                row = context.get("row")
                self.start_download(retry_only=True, only_rows={row} if row is not None else None)

    def remember_folder(self, folder):
        try:
            save_folder(folder)
        except (OSError, ValueError):
            self.log.appendPlainText("这次未能记住保存位置，下次打开时请重新选择；不影响本次下载。")

    def show_failure(self):
        rows = [i.row() for i in self.table.selectionModel().selectedRows() if self.states.get(i.row()) == "失败"]
        if not rows:
            rows = [row for row, state in self.states.items() if state == "失败"]
        if rows:
            row = rows[0]
            item = self.table.item(row, 4)
            self.error_context = {"hint": item.data(Qt.ItemDataRole.UserRole + 1), "operation": "download",
                                  "url": self.videos[row].referer, "row": row}
            self.error_dialog("下载失败原因", self.videos[row].title + "\n\n" + item.toolTip(),
                              item.data(Qt.ItemDataRole.UserRole) or "")

    def browse(self):
        folder = QFileDialog.getExistingDirectory(self, "选择保存 MP4 的文件夹", self.folder.text())
        if folder:
            self.folder.setText(folder)
            self.remember_folder(Path(folder))
            self.refresh_storage_info()

    def browse_cookies(self, checked=False, parent=None):
        dialog_parent = parent or self.network_panel
        try:
            url = validate_url(self.url.text())
        except ValueError:
            QMessageBox.information(dialog_parent, "先填写视频网址", "请先在主界面粘贴目标视频网址，再导入对应网站的登录信息。")
            return False
        path, _ = QFileDialog.getOpenFileName(dialog_parent, "选择导出的 Cookies（不是浏览器数据库）", str(downloads_folder()), "Cookies 导出文件 (*.txt *.json);;所有文件 (*)")
        if path:
            try:
                inspection = inspect_cookie_file(path, url)
            except (OSError, ValueError) as exc:
                QMessageBox.information(dialog_parent, "这个文件暂时不能导入", str(exc))
                return False
            self.clear_browser_session()
            self.import_summary = inspection.summary()
            self.import_site = site_scope(url)
            self.cookies.setText(path)
            self.use_login.setChecked(True)
            self.update_cookie_status()
            self.update_actions()
            return True
        return False

    def accept_cookie_text(self, text):
        url = validate_url(self.url.text())
        inspection = inspect_cookie_text(text, url)
        # Validate before replacing any working session or selected user file.
        session = BrowserSession("clipboard", site_scope(url), tuple(copy.deepcopy(cookie) for cookie in inspection.jar))
        self.clear_browser_session()
        self.cookies.clear()
        self.browser_session = session
        self.use_login.setChecked(True)
        self.update_cookie_status()
        self.update_actions()

    def import_clipboard_cookies(self):
        try:
            self.accept_cookie_text(QApplication.clipboard().text())
        except ValueError as exc:
            QMessageBox.information(self.network_panel, "尚未导入 Cookies", str(exc))
            return
        QMessageBox.information(self.network_panel, "Cookies 已导入", "已导入当前网站的可用 Cookies。可点“检查导入信息”查看本地检查结果，再点“完成并查找视频”；账号权限仍由网站确认。")

    def check_login_information(self):
        if self.worker:
            return
        file_selected = bool(self.cookies.text().strip())
        def report_error(title, message):
            if file_selected:
                self.import_summary = message
                try:
                    self.import_site = site_scope(self.url.text())
                except ValueError:
                    self.import_site = None
                self.update_cookie_status()
            QMessageBox.information(self.network_panel, title, message)
        try:
            url = validate_url(self.url.text())
            if file_selected:
                inspection = inspect_cookie_file(self.cookies.text().strip(), url)
                source = "导出文件（本次已重新读取）"
            elif self.browser_session and self.browser_session.supports(url):
                inspection = inspect_cookie_jar(self.browser_session.cookies, url)
                source = "软件暂存的登录信息"
            else:
                raise ValueError("尚未添加当前网站的登录信息。普通公开视频可直接查找；需要登录时，请读取对应浏览器或导入当前网站的 Cookies。")
        except OSError:
            report_error("登录文件暂时无法读取", "文件可能已移动、删除或不可读取，请重新选择导出文件；也可改用剪贴板导入。")
            return
        except ValueError as exc:
            report_error("登录信息需要检查", str(exc))
            return
        if file_selected:
            self.import_summary = inspection.summary()
            self.import_site = site_scope(url)
            self.update_cookie_status()
        text = (f"## 本地检查结果\n来源：{source}\n\n格式：{inspection.format}\n\n{inspection.summary()}\n\n"
                "此检查只核对本机记录，不连接网站，也不显示 Cookie 名称或内容。有效期尚未结束的记录也可能已被网站撤销。\n\n"
                "确认后关闭检查窗口，点 **完成并查找视频**。若仍提示登录或权限不足，请先确认浏览器中该视频能播放，再重新导出。")
        run_dialog(HelpDialog("登录信息检查 · 仅检查本机记录", text, self.network_panel))

    def lookup_from_settings(self):
        if self.worker:
            return
        self.network_panel.accept()
        QTimer.singleShot(0, self, self.start_analysis)

    def show_cookie_guide(self):
        run_dialog(CookieGuide(self))

    def show_cookie_file_help(self):
        browser = self.cookie_browser.currentData()
        root = browser_root(browser)
        example = root / ("用户资料名/cookies.sqlite" if browser == "firefox" else "Default/Network/Cookies")
        text = f"""## 不用寻找浏览器数据库
建议关闭这个说明，点击 **按步骤获取 Cookies…**，跟着四步指引操作；里面可以打开正确的扩展商店页面、复制商店链接、打开下载文件夹和直接导入。

## Edge / Chrome 最短操作流程
1. 在已经登录的浏览器中打开**目标视频页面**，确认能播放。
2. 安装 **Get cookies.txt LOCALLY**（提供者 **kairi003**），打开视频页右上角的“扩展 / 拼图”图标，点这个扩展。Edge 可以使用 Chrome 商店中的这个扩展；安装时由你确认浏览器权限。
3. 把 **Export Format** 设为 **Netscape**。点 **Copy**，回软件点 **从剪贴板导入**，就不用找文件了。
4. 也可以点 **Export / Export As** 保存 .txt，回软件点 **选择 Cookies 文件**。文件通常在“下载”文件夹，浏览器按 **Ctrl+J**，在刚导出的记录旁点“在文件夹中显示”就能找到。Export As 可以自己指定保存位置。
5. 可点 **检查导入信息** 核对当前网站及有效期，再点 **完成并查找视频**，重新读取主界面的网址，不会自动下载。

不要点 **Export All Cookies**，只导出当前网站；不要选 Header String。已经导出了 JSON？常见的 Cookies 记录列表可直接选择或复制导入，无需转换；只有名称和值的对象不够用，需要同时有网站、路径等信息。Firefox 可用 **cookies.txt** 扩展导出当前网站的 Netscape 文本。商店入口在分步指引中，不必自行搜索同名工具。

## 内部数据库位置（仅排错参考，不用于导入）
浏览器内部数据库在 `{example}`。它可能被占用或加密，**不是导出的 .txt 文件**。Windows 常见位置：
- Edge：`%LOCALAPPDATA%/Microsoft/Edge/User Data/<用户>/Network/Cookies`
- Chrome：`%LOCALAPPDATA%/Google/Chrome/User Data/<用户>/Network/Cookies`
- Firefox：`%APPDATA%/Mozilla/Firefox/Profiles/<用户>/cookies.sqlite`

这些位置只供“读取浏览器”的资料选择参考。Edge 可打开 **edge://version** 查看“个人资料路径”。不需要把数据库复制出来，也不需要打开 F12。

## 直接读取浏览器失败时
“占用”表示数据库暂时打不开：先正常退出该浏览器全部窗口，确认任务管理器中没有后台进程，再重试；软件不会强制关闭浏览器。也可以直接用上面的导出方式。

“无法解密”属于另一种情况，退出浏览器未必能解决。可用文件导出方式或 Firefox；无需管理员运行或关闭浏览器安全功能。

Cookies 相当于登录凭据，只保存在自己的电脑，不要发给他人。原视频在浏览器中也看不了时，导入不会增加权限。

[yt-dlp 官方导出说明与扩展链接](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)
"""
        run_dialog(HelpDialog("Cookies 文件在哪，以及如何导入", text, self.network_panel))

    def show_history(self):
        try:
            dialog = HistoryDialog(self.history, self)
            dialog.relocated.connect(self.update_history_path)
            run_dialog(dialog)
        except (OSError, sqlite3.Error):
            QMessageBox.warning(self, "历史暂时无法读取", "历史记录文件可能被占用或损坏。下载和已有视频文件不受影响。")

    def update_history_path(self, old, new):
        for row, path in list(self.paths.items()):
            if Path(path) == Path(old) and self.states.get(row) == '已完成':
                self.paths[row] = new
                self.table.item(row, 4).setToolTip(new)

    def show_progress_files(self):
        if self.worker or self.storage_worker:
            return
        try:
            folder = Path(self.folder.text()).expanduser()
            if not self.folder.text().strip() or not folder.is_dir():
                QMessageBox.information(self, "先选择保存位置", "请选择曾下载过视频的保存文件夹，再查看未完成文件。")
                return
            dialog = ProgressDialog(folder, self)
            run_dialog(dialog)
            self.refresh_storage_info()
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "暂时无法查看进度", str(exc))

    def check_previous_downloads(self, items):
        try:
            found = self.history.find_many(items)
        except (OSError, sqlite3.Error):
            self.log.appendPlainText("暂时无法检查下载历史，本次继续正常下载。")
            return items
        if not found:
            return items
        dialog = QMessageBox(self)
        dialog.setWindowTitle("有视频已经下载过")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(f"选中的视频里，有 {len(found)} 个已按相同画质选择下载过，且本地文件仍在。")
        dialog.setInformativeText("如果网页内容或可用画质更新了，可以选择“仍然重新下载”。")
        skip = dialog.addButton("使用已有文件", QMessageBox.ButtonRole.AcceptRole)
        again = dialog.addButton("仍然重新下载", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(skip)
        run_dialog(dialog)
        if dialog.clickedButton() == again:
            return items
        if dialog.clickedButton() != skip:
            return []
        for row, record in found.items():
            self.handle_event("row", {"row": row, "state": "已完成", "path": record["path"], "existing": True})
        remaining = [item for item in items if item[0] not in found]
        if not remaining:
            self.set_phase("使用已有文件", "success")
            self.task_label.setText(f"这 {len(found)} 个视频已下载，无需再次传输。双击对应项目即可播放。")
            self.progress_label.setText("若需要新的副本，可重新勾选并选择“仍然重新下载”。")
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
        return remaining

    def refresh_storage_info(self):
        if self.worker or self.storage_worker:
            return
        text = self.folder.text().strip()
        try:
            exists = bool(text) and Path(text).expanduser().is_dir()
        except (OSError, ValueError):
            exists = False
        if not exists:
            self.storage_hint.setText('保存文件夹尚不存在或暂不可用；下载前会检查并尝试创建。')
            return
        worker = StorageInfoWorker(Path(text).expanduser().resolve())
        self.storage_worker = worker
        self.storage_hint.setText('正在检查剩余空间与未完成素材占用…')
        self.storage_hint.setToolTip('只检查当前保存位置和本软件保留的未完成素材，不扫描全盘。读数是检查时的状态；空间不足时不会自动删除文件。')
        worker.finished.connect(self.storage_info_finished)
        self.update_actions()
        worker.start()

    def storage_info_finished(self):
        worker = self.storage_worker
        self.storage_worker = None
        try:
            same_folder = Path(self.folder.text()).expanduser().resolve() == worker.folder
        except (OSError, ValueError):
            same_folder = False
        if same_folder:
            if worker.result:
                free, used, filesystem = worker.result
                limit = ' · FAT32 单文件上限约 4 GB' if filesystem.upper() == 'FAT32' else ''
                self.storage_hint.setText(f'保存盘可用 {bytes_label(free)} · 未完成素材占用 {bytes_label(used)}（最近检查）{limit}')
            else:
                self.storage_hint.setText('暂时无法读取空间或进度，请检查磁盘连接，稍后再刷新。')
                self.storage_hint.setToolTip(redact_detail(worker.error))
        worker.deleteLater()
        self.update_actions()
        if self.close_pending:
            self.close()

    def show_login_help(self):
        run_dialog(HelpDialog("登录与浏览器读取帮助", LOGIN_HELP, self.network_panel))

    def open_folder(self):
        folder = Path(self.folder.text()).expanduser()
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))
        else:
            QMessageBox.information(self, "文件夹尚未创建", "选择文件夹或完成一次下载后即可打开。")

    def open_video(self, row, column):
        if column == 5:
            self.show_result_notices(row)
            return
        if self.states.get(row) != "已完成" or row not in self.paths:
            return
        path = Path(self.paths[row])
        if not path.is_file():
            QMessageBox.information(self, "视频文件找不到了", "文件可能被移动或删除。可到新位置查找，或重新勾选这个视频下载。")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.information(self, "未能打开播放器", "可以点击“打开文件夹”，用本机播放器打开该 MP4 文件。")

    def closeEvent(self, event):
        if self.system_dialog and (self.system_dialog.job or self.system_dialog.engine_panel.job):
            self.close_pending = True
            self.system_dialog.reject()
            event.ignore()
        elif self.worker:
            self.close_pending = True
            self.cancel_task()
            event.ignore()
        elif self.storage_worker:
            self.close_pending = True
            event.ignore()
        elif self.health_worker:
            self.close_pending = True
            self.task_label.setText('正在结束本机环境检查，请稍候…')
            event.ignore()
        else:
            self.persist_queue()
            self.browser_session = None
            event.accept()


def main():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("拾影 · 视频下载器")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyle("Fusion")
    from .engine_state import EngineError, confirm_start
    window = MainWindow()
    window.show()
    window.refresh_storage_info()
    window.begin_health_check()
    from .bootstrap import ENGINE_NOTICE
    if ENGINE_NOTICE:
        window.log.appendPlainText(ENGINE_NOTICE)
        window.progress_label.setText(ENGINE_NOTICE)
    try:
        confirm_start()
    except (EngineError, OSError):
        window.log.appendPlainText('本次窗口已打开，但引擎启动确认暂时无法保存；下次启动可能恢复之前版本。')
    return app.exec()

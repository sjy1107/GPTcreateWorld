"""Manual, restart-based engine updates in the existing management window."""
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from .engine_state import EngineError, read_state, schedule_restore
from .engine_update import check_update, current_version, install_candidate
from .network_retry import Cancelled


class EngineJob(QThread):
    progress = Signal(str)

    def __init__(self, operation, *, root, channel='stable', candidate=None, proxy=''):
        super().__init__()
        self.operation, self.root, self.channel = operation, root, channel
        self.candidate, self.proxy = candidate, proxy
        self.cancel = threading.Event()
        self.result, self.error = None, ''

    def run(self):
        try:
            if self.operation == 'check':
                self.result = check_update(self.channel, self.proxy, self.cancel)
            elif self.operation == 'install':
                self.result = install_candidate(self.candidate, self.cancel, self.progress.emit, self.proxy, self.root)
            else:
                self.result = schedule_restore(self.root)
        except Cancelled:
            self.error = '已取消本次更新操作，当前引擎保持不变。'
        except EngineError as error:
            self.error = str(error)
        except Exception:
            self.error = '本次操作未完成，当前引擎保持不变。可能是网络中断、文件被占用或验证组件不可用，请检查环境后重试。'


class EnginePanel(QWidget):
    idle = Signal()

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.root = Path(window.history.path).parent / 'engine'
        self.job = None
        self.candidate = None
        self.available = False
        layout = QVBoxLayout(self)
        intro = QLabel('仅在你点击时连接官方发布源（PyPI）检查或下载解析引擎，不读取账号。新版先独立验证，关闭并重新打开软件后才启用；原环境与之前的版本保留。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.version_label = QLabel()
        self.version_label.setTextFormat(Qt.TextFormat.PlainText)
        self.version_label.setWordWrap(True)
        layout.addWidget(self.version_label)
        row = QHBoxLayout()
        self.channel = QComboBox()
        self.channel.addItem('稳定版（推荐）', 'stable')
        self.channel.addItem('预发布版（网站修复可能较快）', 'preview')
        self.channel.setToolTip('当前版本若是较新的预发布版，可能比最新稳定版还新；检查不会自动降级。')
        self.channel.currentIndexChanged.connect(self.channel_changed)
        row.addWidget(self.channel)
        self.check_button = QPushButton('检查官方更新')
        self.check_button.clicked.connect(lambda: self.begin('check'))
        row.addWidget(self.check_button)
        self.install_button = QPushButton('下载、验证并安排下次启用')
        self.install_button.clicked.connect(lambda: self.begin('install'))
        row.addWidget(self.install_button)
        layout.addLayout(row)
        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)
        self.detail.setPlainText('1. 选择版本类型，点“检查官方更新”。\n\n2. 有新版本时，点“下载、验证并安排下次启用”。软件会校验下载文件，并用本机测试视频检查解析、下载和 MP4 输出，不访问你的视频或账号。\n\n3. 成功后关闭并重新打开软件，新版本才生效。若新版无法通过启动检查，会回到之前的引擎。\n\n新版可能要求更新附加组件；验证失败时不会更改当前引擎，可先查看“环境检查”。这里只更新 yt-dlp，不修改其他 Python 组件，也不会保证某个网站一定可下载。\n\n已有排队结果会在关闭后按原队列设置处理；需要时先开启“记住队列”。视频与 Cookies 不会因引擎更新被清除。')
        layout.addWidget(self.detail, 1)
        bottom = QHBoxLayout()
        self.restore_button = QPushButton('恢复之前的引擎')
        self.restore_button.clicked.connect(lambda: self.begin('restore'))
        bottom.addWidget(self.restore_button)
        self.notes_button = QPushButton('查看官方更新说明')
        self.notes_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl('https://github.com/yt-dlp/yt-dlp/releases')))
        bottom.addWidget(self.notes_button)
        self.cancel_button = QPushButton('取消本次操作')
        self.cancel_button.clicked.connect(self.cancel_operation)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)
        self.status = QLabel('尚未联网检查。')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.refresh()

    def channel_changed(self, *_):
        self.candidate, self.available = None, False
        self.status.setText('版本类型已更改，请重新检查；当前引擎没有改变。')
        self.refresh()

    def refresh(self):
        try:
            state = read_state(self.root)
            current = current_version()
            pending = state['pending'] is not None
            self.version_label.setText('当前窗口使用：' + current + ('\n已有安排，关闭并重新打开软件后生效。' if pending else '') +
                                       ('\n' + state['notice'] if state['notice'] else '') + '\n管理位置：' + str(self.root))
            can_restore = pending or (state['previous'] is not None and state['previous'] != state['active'])
        except Exception:
            pending, can_restore = True, True
            self.version_label.setText('版本记录暂时不可读，请先保留文件并查看环境检查；当前窗口仍使用已载入的引擎。')
        busy = self.job is not None
        self.check_button.setEnabled(not busy)
        self.channel.setEnabled(not busy)
        self.install_button.setEnabled(not busy and self.available and self.candidate is not None and not pending)
        self.restore_button.setText('取消待启用 / 修复版本记录' if pending else '下次启动恢复之前的引擎')
        self.restore_button.setEnabled(not busy and can_restore)
        self.cancel_button.setEnabled(busy)

    def begin(self, operation):
        if self.job or self.window.worker or self.window.storage_worker:
            return
        if operation == 'install' and not (self.candidate and self.available):
            return
        if operation == 'check':
            self.candidate, self.available = None, False
        self.job = EngineJob(operation, root=self.root, channel=self.channel.currentData(), candidate=self.candidate,
                             proxy=self.window.proxy.text().strip())
        self.job.progress.connect(self.status.setText)
        self.job.finished.connect(self.finished)
        self.status.setText('正在检查官方版本…' if operation == 'check' else '正在处理引擎版本，请稍候…')
        self.refresh()
        self.job.start()

    def finished(self):
        job, self.job = self.job, None
        if job.error:
            self.status.setText(job.error)
        elif job.operation == 'check':
            self.candidate = job.result['candidate']
            self.available = job.result['available']
            self.status.setText(('发现可更新版本：' if self.available else '没有比当前窗口更新的可用版本。当前环境可用的最新版本：') +
                                self.candidate.version + '。检查结果仅针对所选类型；不会自动降级。')
        elif job.operation == 'install':
            self.available = False
            self.status.setText('新版已通过校验和本机 MP4 测试。请关闭并重新打开软件后使用；当前窗口仍使用原版本。')
        else:
            self.status.setText(job.result['notice'])
            self.available = False
        self.refresh()
        job.deleteLater()
        self.idle.emit()

    def cancel_operation(self):
        if self.job:
            self.job.cancel.set()
            self.status.setText('正在取消，请等待当前请求或本机验证结束；已经安排成功的版本可用“取消待启用”撤销。')

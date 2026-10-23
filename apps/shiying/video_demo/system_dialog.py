"""One local-only entry for environment status and data management."""
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QDialog, QHeaderView, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QTabWidget, QTextBrowser, QVBoxLayout, QWidget)
from . import preferences
from .bootstrap import ROOT, repair_steps
from .data_inventory import clear_record_file, inventory
from .diagnostics import diagnostic_text
from .health import environment_checks
from .transfer import bytes_label
from .engine_panel import EnginePanel
from .paths import frozen

LABELS = {'ok': '正常', 'warning': '需注意', 'error': '需处理', 'info': '说明'}


class LocalJob(QThread):
    def __init__(self, function, *args, **kwargs):
        super().__init__()
        self.function, self.args, self.kwargs = function, args, kwargs
        self.result = None
        self.error = ''

    def run(self):
        try:
            self.result = self.function(*self.args, **self.kwargs)
        except Exception:
            self.error = '本机检查或记录操作暂时未完成。可能是文件被占用、损坏或位置不可访问，请稍后重试。'


class SystemDialog(QDialog):
    def __init__(self, window, tab=0):
        super().__init__(window)
        self.window = window
        self.job = None
        self.operation = ''
        self.closing = False
        self.route = ''
        self.rows = []
        self.completed_action = ''
        self.setWindowTitle('检查与数据 · 环境、记录与解析引擎')
        self.resize(950, 650)
        layout = QVBoxLayout(self)
        intro = QLabel('环境检查和数据管理只在本机进行。解析引擎页仅在你点击检查或下载时联网，不读取账号内容。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        checks_page = QWidget()
        checks_layout = QVBoxLayout(checks_page)
        self.check_table = self.make_table(['检查项目', '结果', '说明'])
        checks_layout.addWidget(self.check_table, 1)
        self.check_detail = QTextBrowser()
        self.check_detail.setOpenLinks(False)
        self.check_detail.setMaximumHeight(160)
        checks_layout.addWidget(self.check_detail)
        actions = QHBoxLayout()
        self.recheck = QPushButton('重新检查')
        self.recheck.clicked.connect(self.begin_checks)
        actions.addWidget(self.recheck)
        self.check_action = QPushButton('查看处理位置')
        self.check_action.clicked.connect(self.handle_check_action)
        actions.addWidget(self.check_action)
        self.copy = QPushButton('复制检查摘要')
        self.copy.setToolTip('只在点击时复制经过隐藏本机完整路径的检查结果；不会自动发送。')
        self.copy.clicked.connect(self.copy_checks)
        actions.addWidget(self.copy)
        checks_layout.addLayout(actions)
        self.tabs.addTab(checks_page, '环境检查')
        data_page = QWidget()
        data_layout = QVBoxLayout(data_page)
        self.data_table = self.make_table(['数据类别', '磁盘占用', '状态 / 数量'])
        data_layout.addWidget(self.data_table, 1)
        self.data_detail = QTextBrowser()
        self.data_detail.setOpenLinks(False)
        self.data_detail.setMaximumHeight(155)
        data_layout.addWidget(self.data_detail)
        data_actions = QHBoxLayout()
        self.refresh = QPushButton('刷新数据概况')
        self.refresh.clicked.connect(self.begin_inventory)
        data_actions.addWidget(self.refresh)
        self.open_location = QPushButton('打开所在文件夹')
        self.open_location.clicked.connect(self.open_selected_location)
        data_actions.addWidget(self.open_location)
        self.manage = QPushButton('查看 / 管理…')
        self.manage.clicked.connect(self.manage_selected)
        data_actions.addWidget(self.manage)
        self.clear = QPushButton('清除这类记录…')
        self.clear.clicked.connect(self.clear_selected)
        data_actions.addWidget(self.clear)
        data_layout.addLayout(data_actions)
        note = QLabel('数据概况按需在后台统计。已完成的 MP4 在此只读；清除历史不删除视频，清理未完成素材需要另外勾选确认。')
        note.setWordWrap(True)
        data_layout.addWidget(note)
        self.tabs.addTab(data_page, '本机数据')
        self.engine_panel = EnginePanel(window, self)
        self.engine_panel.idle.connect(self.engine_idle)
        self.tabs.addTab(self.engine_panel, '解析引擎更新')
        self.check_table.itemSelectionChanged.connect(self.show_check)
        self.data_table.itemSelectionChanged.connect(self.show_data)
        self.tabs.currentChanged.connect(self.tab_changed)
        close = QPushButton('关闭')
        close.clicked.connect(self.reject)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.tabs.setCurrentIndex(tab)
        self.show_checks()
        if tab == 1 and not self.job:
            self.begin_inventory()

    @staticmethod
    def make_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        return table

    @staticmethod
    def fill(table, values):
        table.setRowCount(len(values))
        for row, fields in enumerate(values):
            for column, value in enumerate(fields):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                table.setItem(row, column, item)
            table.setRowHeight(row, 38)
        if values:
            table.selectRow(0)

    def set_busy(self, busy):
        for widget in (self.recheck, self.refresh, self.check_table, self.data_table, self.copy):
            widget.setEnabled(not busy)
        if busy:
            for widget in (self.clear, self.manage, self.open_location, self.check_action):
                widget.setEnabled(False)
        else:
            self.show_check()
            self.show_data()

    def begin(self, operation, function, *args, **kwargs):
        if self.job:
            return
        self.operation = operation
        self.job = LocalJob(function, *args, **kwargs)
        self.job.finished.connect(self.finished_job)
        self.status.setText('正在本机处理，请稍候…')
        self.set_busy(True)
        self.job.start()

    def begin_checks(self):
        if self.window.health_worker:
            self.status.setText('启动检查仍在进行，完成后会显示结果；不需要重复运行。')
            return
        self.checked_folder = self.window.folder.text()
        self.begin('checks', environment_checks, self.checked_folder, preferences.PREFERENCES,
                   self.window.history.path, self.window.queue_store.path)

    def begin_inventory(self):
        if not self.window.folder.text().strip():
            self.status.setText('先在主界面填写保存位置，才能统计该位置的素材和视频。')
            return
        self.begin('inventory', inventory, Path(self.window.folder.text()), preferences.PREFERENCES,
                   self.window.history.path, self.window.queue_store.path, ROOT / ('data' if frozen() else '.local') / 'startup.log',
                   has_session=self.window.browser_session is not None, cookie_path=self.window.cookies.text(),
                   log_lines=self.window.log.document().blockCount() if self.window.log.toPlainText() else 0)

    def finished_job(self):
        job, operation = self.job, self.operation
        self.job = None
        if job.error:
            self.status.setText(job.error + ' 原视频文件不受此操作影响。')
        elif operation == 'checks':
            self.window.accept_health_results(job.result, self.checked_folder)
            self.show_checks()
        elif operation == 'inventory':
            self.rows = job.result
            self.fill(self.data_table, [(row.title, bytes_label(row.size) if row.size is not None else ('见版本管理页' if row.key == 'engine' else '仅内存 / 外部文件不计入' if row.key == 'session' else '仅内存' if row.key == 'log' else '暂不可读'), row.summary) for row in self.rows])
            self.status.setText(self.completed_action + '已刷新。文件占用是本次检查时的读数，不会持续扫描磁盘。')
            self.completed_action = ''
        else:
            if operation == 'queue':
                self.window.queue_unresolved = []
                self.window.queue_issues = []
                self.window.refresh_queue_button()
            self.status.setText('已清除选定类别的记录；已完成视频没有被删除。')
            self.completed_action = '已清除选定类别的记录，视频保留。'
        self.set_busy(False)
        job.deleteLater()
        if self.closing:
            self.reject()
        elif operation not in ('checks', 'inventory') and not job.error:
            self.begin_inventory()

    def show_checks(self):
        checks = self.window.health_results
        self.fill(self.check_table, [(item.title, LABELS[item.state], item.summary) for item in checks])
        if self.window.health_worker:
            self.status.setText('启动检查正在后台进行，可以先查看数据或阅读说明。')
        elif not checks:
            self.status.setText('尚未检查，点击“重新检查”。只在本机检查，不请求视频网站。')
        else:
            errors = sum(item.state == 'error' for item in checks)
            warnings = sum(item.state == 'warning' for item in checks)
            self.status.setText(f'检查结束：{errors} 项需处理，{warnings} 项需注意。可选组件提醒不代表所有视频都不能下载。')
        self.show_check()

    def show_check(self):
        index = self.check_table.currentRow()
        checks = self.window.health_results
        item = checks[index] if 0 <= index < len(checks) else None
        self.check_detail.setPlainText((item.title + '\n\n' + item.detail + ('\n\n' + repair_steps() if item.state == 'error' else '')) if item else '选中一项，查看检查含义和处理步骤。')
        self.check_action.setEnabled(bool(item and item.action) and not self.job)

    def handle_check_action(self):
        index = self.check_table.currentRow()
        if not 0 <= index < len(self.window.health_results):
            return
        action = self.window.health_results[index].action
        if action == 'data':
            self.tabs.setCurrentIndex(1)
        elif action == 'folder':
            self.route = 'storage'
            self.accept()

    def show_data(self):
        index = self.data_table.currentRow()
        item = self.rows[index] if 0 <= index < len(self.rows) else None
        self.data_detail.setPlainText(item.title + '\n位置：' + item.location + '\n\n' + item.detail if item else '选中一类数据查看位置和影响；清除前需要确认。')
        self.clear.setText('选择素材清理…' if item and item.key == 'partials' else '清除这类记录…')
        self.clear.setEnabled(bool(item and item.clear) and not self.job)
        self.manage.setEnabled(bool(item and item.view) and not self.job)
        self.open_location.setEnabled(bool(item and item.key not in ('session', 'log')) and not self.job)

    def open_selected_location(self):
        index = self.data_table.currentRow()
        if not 0 <= index < len(self.rows):
            return
        item = self.rows[index]
        if item.key in ('session', 'log'):
            return
        try:
            path = Path(item.location)
            folder = path if path.is_dir() else path.parent
            while not folder.is_dir() and folder != folder.parent:
                folder = folder.parent
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                self.status.setText('未能打开文件夹，可复制详情中的位置到文件资源管理器。')
        except (OSError, ValueError):
            self.status.setText('文件夹暂不可访问，请先确认磁盘连接和目录权限。')

    def manage_selected(self):
        index = self.data_table.currentRow()
        if not 0 <= index < len(self.rows):
            return
        action = self.rows[index].view
        if action == 'engine':
            self.tabs.setCurrentIndex(2)
            return
        if action == 'preferences':
            action = 'storage'
        if action:
            self.route = action
            self.accept()

    def clear_selected(self):
        if self.job or self.window.worker or self.window.storage_worker:
            return
        index = self.data_table.currentRow()
        if not 0 <= index < len(self.rows):
            return
        item = self.rows[index]
        if not item.clear:
            return
        if item.key == 'partials':
            self.route = 'partials'
            self.accept()
            return
        message = '将清除：' + item.title + '\n\n' + item.detail + '\n\n只处理这一类数据，不删除已完成的 MP4。是否继续？'
        question = QMessageBox(QMessageBox.Icon.Question, '确认清除这类记录？', message,
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        question.setTextFormat(Qt.TextFormat.PlainText)
        question.setDefaultButton(QMessageBox.StandardButton.No)
        try:
            answer = question.exec()
        finally:
            question.deleteLater()
        if answer != QMessageBox.StandardButton.Yes:
            return
        if item.key == 'session':
            self.window.clear_login_information()
            self.completed_action = '已清除软件暂存的登录信息，原导出文件保留。'
            self.begin_inventory()
        elif item.key == 'log':
            self.window.log.clear()
            self.completed_action = '已清空当前窗口运行记录。'
            self.begin_inventory()
        elif item.key == 'history':
            self.begin('history', self.window.history.remove)
        elif item.key == 'queue':
            self.window.queue_timer.stop()
            self.begin('queue', self.window.queue_store.clear)
        elif item.key == 'preferences':
            self.begin('preferences', clear_record_file, preferences.PREFERENCES)
        elif item.key == 'startup_log':
            self.begin('startup_log', clear_record_file, ROOT / ('data' if frozen() else '.local') / 'startup.log')

    def copy_checks(self):
        text = '拾影 · 本机环境检查摘要\n' + '\n'.join(item.title + '：' + LABELS[item.state] + ' · ' + item.summary for item in self.window.health_results)
        QApplication.clipboard().setText(diagnostic_text(text))
        self.status.setText('已复制检查摘要，不包含原网址、登录内容或完整本机路径；不会自动发送。')

    def tab_changed(self, index):
        self.status.setVisible(index != 2)
        if index == 1 and not self.rows and not self.job:
            self.begin_inventory()

    def reject(self):
        if self.engine_panel.job:
            self.closing = True
            self.engine_panel.cancel_operation()
            return
        if self.job:
            self.closing = True
            self.status.setText('正在结束本机操作，请稍候；已确认的记录操作会先完成。')
            return
        super().reject()

    def accept(self):
        if self.engine_panel.job:
            self.closing = True
            self.engine_panel.cancel_operation()
            return
        if self.job:
            self.closing = True
            self.status.setText('正在结束本机操作，请稍候。')
            return
        super().accept()

    def closeEvent(self, event):
        if self.engine_panel.job:
            self.closing = True
            self.engine_panel.cancel_operation()
            event.ignore()
            return
        if self.job:
            self.closing = True
            event.ignore()
            self.status.setText('正在结束本机操作，请稍候。')
        else:
            super().closeEvent(event)

    def engine_idle(self):
        if self.closing:
            self.reject()

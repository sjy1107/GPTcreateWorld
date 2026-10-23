from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from PySide6.QtCore import Qt, QUrl, QThread, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QFileDialog, QHeaderView, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout
from .history import file_state, inspect_relocation

FILE_LABELS = {"unchecked": "未检查", "ready": "文件在原位置", "missing": "文件找不到", "changed": "文件大小已变化", "unreadable": "暂时无法读取"}
FILE_HINTS = {"unchecked": "点击“刷新文件状态”核对当前搜索结果；播放前也会重新检查。",
              "ready": "文件存在且大小与保存记录一致；这不是完整解码检查。",
              "missing": "文件可能已移动或删除；可选中记录，点击“重新定位文件”关联新的位置。移除记录不会删除文件。",
              "changed": "大小与下载完成时不同，可能被修改或截断；请先在本机核实。",
              "unreadable": "可能是权限、磁盘或路径暂时不可用；不会当作失效记录自动移除。"}


class RelinkWorker(QThread):
    def __init__(self, record, path):
        super().__init__()
        self.record, self.path = dict(record), path
        self.result = None
        self.error = ''

    def run(self):
        try:
            self.result = inspect_relocation(self.record, self.path)
        except Exception as exc:
            self.error = str(exc)


class HistoryDialog(QDialog):
    relocated = Signal(str, str)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.file_states = {}
        self.relink_worker = None
        self.action_buttons = []
        self.setWindowTitle("下载历史")
        self.resize(900, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(QLabel("只记录成功保存的视频，最多保留 500 条。不保存原网址或登录信息。"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索视频名称或文件位置")
        self.search.setToolTip("只搜索保存在本机的下载记录，不会联网搜索视频。")
        self.search.textChanged.connect(self.populate)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search, 1)
        self.check_button = QPushButton("刷新文件状态")
        self.check_button.setToolTip("只检查当前搜索结果的文件位置和大小，不联网、不扫描文件内容。")
        self.check_button.clicked.connect(self.refresh_file_status)
        search_row.addWidget(self.check_button)
        self.prune_button = QPushButton("移除失效记录")
        self.prune_button.setToolTip("仅移除当前搜索结果中文件缺失或大小已变化的记录。\n不会删除视频；暂时无法读取的记录会保留。")
        self.prune_button.clicked.connect(self.remove_unavailable)
        search_row.addWidget(self.prune_button)
        layout.addLayout(search_row)
        self.status_hint = QLabel("文件状态按需检查，搜索时不会反复访问磁盘。")
        self.status_hint.setWordWrap(True)
        layout.addWidget(self.status_hint)
        self.empty_hint = QLabel("")
        layout.addWidget(self.empty_hint)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["视频", "画质选择", "保存时间", "文件位置", "文件状态"])
        self.table.horizontalHeaderItem(1).setToolTip("这是下载时选择的画质上限，不代表实际视频一定达到这个分辨率。")
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(4, 130)
        self.table.cellDoubleClicked.connect(lambda *_: self.open_file())
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, callback in (("播放视频", self.open_file), ("打开文件夹", self.open_folder), ('重新定位文件', self.relink_file),
                               ("移除此条", self.remove_selected), ("清空记录", self.clear_records)):
            button = QPushButton(text)
            if text in ("移除此条", "清空记录"):
                button.setToolTip("只移除下载历史记录，绝不会删除已经保存的视频文件。")
            elif text == "打开文件夹":
                button.setToolTip("打开当时的保存目录；如果文件被移动，请到新的位置查找。")
            elif text == '重新定位文件':
                button.setToolTip('手动选择移动后的 MP4；核对大小、视频格式及已有的抽样指纹，确认后只更新记录，不搬移文件。')
            button.clicked.connect(callback)
            row.addWidget(button)
            self.action_buttons.append(button)
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        note = QLabel('文件移动后可“重新定位文件”。新记录用少量内容抽样辅助核对，旧记录需你确认；不扫描全盘，不做完整解码检查。移除记录不会删除视频。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.refresh()

    def refresh(self):
        self.records = self.store.recent()
        keys = {self.record_key(record) for record in self.records}
        self.file_states = {key: value for key, value in self.file_states.items() if key in keys}
        self.populate()

    @staticmethod
    def record_key(record):
        return (record["id"], record["path"], record["size"])

    def populate(self, *_):
        query = self.search.text().casefold().strip()
        self.visible_records = [record for record in self.records if query in record["title"].casefold() or query in record["path"].casefold()]
        self.table.setRowCount(len(self.visible_records))
        self.empty_hint.setText("没有符合搜索条件的记录。" if self.records else "还没有下载记录。视频成功保存后，会自动出现在这里。")
        self.empty_hint.setVisible(not self.visible_records)
        for row, record in enumerate(self.visible_records):
            try:
                stamp = datetime.fromisoformat(record["finished"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError, OverflowError, OSError):
                stamp = "时间未知"
            state = self.file_states.get(self.record_key(record), "unchecked")
            values = [record["title"], f"≤ {record['quality']}p" if record["quality"] else "最高 / 原画质", stamp, record["path"], FILE_LABELS[state]]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
            self.table.item(row, 4).setToolTip(FILE_HINTS[state])
            self.table.setRowHeight(row, 40)
        self.check_button.setEnabled(bool(self.visible_records))
        self.prune_button.setEnabled(bool(self.visible_records))
        checked = sum(self.record_key(record) in self.file_states for record in self.visible_records)
        self.status_hint.setText(f"当前显示 {len(self.visible_records)} 条 · 已检查 {checked} 条。点“刷新文件状态”核对位置和大小。")

    def selected(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self.visible_records):
            QMessageBox.information(self, "先选择记录", "请在列表中选中一个视频。")
            return None
        return self.visible_records[row]

    def open_file(self):
        record = self.selected()
        if record:
            state = file_state(record)
            self.file_states[self.record_key(record)] = state
            path = Path(record["path"])
            if state == "ready":
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                    QMessageBox.information(self, "没有打开播放器", "请在文件夹中手动打开视频，或设置 MP4 的默认播放器。")
            else:
                QMessageBox.information(self, FILE_LABELS[state], FILE_HINTS[state] + "\n可以打开文件夹核实，或重新粘贴原链接下载。")
            self.populate()

    def open_folder(self):
        record = self.selected()
        if record:
            try:
                folder = Path(record["path"]).parent
                if folder.is_dir():
                    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                        QMessageBox.information(self, "没有打开文件夹", "请使用文件资源管理器手动打开记录中的文件位置。")
                else:
                    QMessageBox.information(self, "文件夹不存在", "原保存文件夹已被移动或删除。")
            except (OSError, ValueError, TypeError):
                QMessageBox.information(self, "文件夹不存在", "原保存文件夹已被移动或删除。")

    def refresh_file_status(self):
        for record in self.visible_records:
            self.file_states[self.record_key(record)] = file_state(record)
        self.populate()
        self.status_hint.setText(f"已检查当前搜索结果中的 {len(self.visible_records)} 条记录；只核对位置和大小，不验证解码。")

    def remove_unavailable(self):
        self.refresh_file_status()
        ids = [record["id"] for record in self.visible_records
               if self.file_states[self.record_key(record)] in {"missing", "changed"}]
        if not ids:
            QMessageBox.information(self, "没有可移除的失效记录", "当前搜索结果中没有文件缺失或大小变化的记录；暂时无法读取的记录会保留。")
            return
        answer = QMessageBox.question(self, "移除这些历史记录？", f"将移除当前搜索结果中的 {len(ids)} 条缺失或大小已变化记录。\n只删除记录，不删除任何视频文件。",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self.store.remove_many(ids)
                self.refresh()
            except (OSError, ValueError, sqlite3.Error):
                QMessageBox.warning(self, "未能更新历史", "记录文件暂时不可写，原记录和视频不受影响，请稍后再试。")

    def remove_selected(self):
        record = self.selected()
        if record:
            self.change_records(record["id"])

    def relink_file(self):
        if self.relink_worker:
            return
        record = self.selected()
        if not record:
            return
        path, _ = QFileDialog.getOpenFileName(self, '选择移动后的原视频文件', str(Path(record['path']).parent), 'MP4 视频 (*.mp4 *.MP4)')
        if not path:
            return
        worker = RelinkWorker(record, path)
        self.relink_worker = worker
        self.set_relink_busy(True)
        self.status_hint.setText('正在本机核对文件大小、视频格式和内容抽样，请稍候…')
        worker.finished.connect(self.relink_finished)
        worker.start()

    def set_relink_busy(self, busy):
        for widget in [self.search, self.table, self.check_button, self.prune_button, *self.action_buttons]:
            widget.setEnabled(not busy)

    def relink_finished(self):
        worker = self.relink_worker
        self.relink_worker = None
        self.set_relink_busy(False)
        try:
            if worker.error:
                QMessageBox.warning(self, '未能重新关联', worker.error)
                self.status_hint.setText('未更改原记录，请检查选择的文件。')
                return
            candidate = worker.result
            detail = ('文件大小与已有的内容抽样指纹一致。' if candidate.matched else
                      '这是一条旧记录，没有保存内容指纹；目前只核对了大小和视频格式，无法据此确认是同一视频。请自行确认选对了原文件。')
            message = detail + '\n\n新位置：' + str(candidate.path) + '\n\n只更新这条记录的位置，不移动、复制或改写视频。这不是完整解码检查。'
            dialog = QMessageBox(QMessageBox.Icon.Question, '关联到这个文件？', message,
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            try:
                answer = dialog.exec()
            finally:
                dialog.deleteLater()
            if answer != QMessageBox.StandardButton.Yes:
                self.status_hint.setText('已取消关联，原记录未更改。')
                return
            self.store.relocate(worker.record, candidate)
            self.relocated.emit(worker.record['path'], str(candidate.path))
            self.search.clear()
            self.refresh()
            for row, record in enumerate(self.visible_records):
                if record['id'] == worker.record['id']:
                    self.file_states[self.record_key(record)] = 'ready'
                    self.populate()
                    self.table.selectRow(row)
                    break
            self.status_hint.setText('已更新文件位置，之后播放和检查会使用新位置；视频文件未改动。')
        except (OSError, ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, '未能更新记录', str(exc))
        finally:
            worker.deleteLater()

    def reject(self):
        if self.relink_worker:
            self.status_hint.setText('正在核对文件，请等检查结束后关闭。')
            return
        super().reject()

    def accept(self):
        if self.relink_worker:
            self.status_hint.setText('正在核对文件，请等检查结束后关闭。')
            return
        super().accept()

    def closeEvent(self, event):
        if self.relink_worker:
            event.ignore()
            self.status_hint.setText('正在核对文件，请等检查结束后关闭。')
        else:
            super().closeEvent(event)

    def clear_records(self):
        if QMessageBox.question(self, "清空下载历史？", "只清空记录，不会删除视频文件。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.change_records()

    def change_records(self, record_id=None):
        try:
            self.store.remove(record_id)
            self.refresh()
        except (OSError, sqlite3.Error):
            QMessageBox.warning(self, "未能更新历史", "记录文件暂时不可写。视频文件不会受影响，请稍后再试。")

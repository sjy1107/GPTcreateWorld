"""Queue recovery is an explicit action, with opt-in local URL storage."""
from urllib.parse import urlsplit
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView,
)


class QueueDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("任务队列 · 关闭后继续处理")
        self.resize(740, 520)
        layout = QVBoxLayout(self)
        self.remember = QCheckBox("在本机记住未完成任务（可选）")
        self.remember.setChecked(window.queue_store.enabled)
        layout.addWidget(self.remember)
        explanation = QLabel("默认关闭。开启后会保存原始网址、标题、画质、命名设置和保存位置，不保存 Cookies 或解析出的临时媒体地址。\n"
                             "记住当前列表中已勾选、未完成的项目；成功查找新链接后会替换队列。恢复后只显示列表，不自动下载。")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["视频", "网站", "原选画质"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, 100)
        self.table.cellDoubleClicked.connect(self.fill_url)
        layout.addWidget(self.table, 1)
        self.issues = QPlainTextEdit()
        self.issues.setReadOnly(True)
        self.issues.setMaximumHeight(130)
        self.issues.setPlainText("\n\n".join(window.queue_issues))
        self.issues.setVisible(bool(window.queue_issues))
        layout.addWidget(self.issues)
        note = QLabel("双击可填入原链接。按住 Ctrl / Shift 可多选后移出队列；当前列表对应项会取消勾选，不删除视频或未完成素材。")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox()
        self.restore = buttons.addButton("重新查找并恢复列表", QDialogButtonBox.ButtonRole.ActionRole)
        self.remove = buttons.addButton("移出选中任务", QDialogButtonBox.ButtonRole.ActionRole)
        self.remove.setToolTip("只移除选中的队列记录，保留其他任务及队列记忆。\n不删除视频或续传素材；移出后会清除上次恢复提示，剩余项可重新查找。")
        self.remove.clicked.connect(self.remove_selected)
        self.table.itemSelectionChanged.connect(self.update_selection)
        self.clear = buttons.addButton("清除并关闭记忆", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        self.restore.clicked.connect(self.restore_clicked)
        self.clear.clicked.connect(lambda: self.remember.setChecked(False) if self.remember.isChecked() else self.toggle(False))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.remember.toggled.connect(self.toggle)
        self.refresh()

    def refresh(self):
        store = self.window.queue_store
        items = store.snapshot["items"]
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            for column, text in enumerate((item["title"], urlsplit(item["url"]).hostname or "", f"{item['quality']}p" if item["quality"] else "最高画质")):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                self.table.setItem(row, column, cell)
        self.restore.setEnabled(bool(items))
        self.update_selection()
        self.clear.setEnabled(store.enabled or store.load_error)
        self.issues.setPlainText("\n\n".join(self.window.queue_issues))
        self.issues.setVisible(bool(self.window.queue_issues))
        self.status.setText("队列记录暂时无法读取，可清除后重新开启；视频文件不受影响。" if store.load_error else
                            f"已记住 {len(items)} 项。恢复时会重新读取网页并核对视频与画质；登录信息需重新添加。" if items else
                            "尚无记住的任务。开启后，勾选想下载的视频即可；下载成功的项目会自动移出。")

    def toggle(self, enabled):
        if not self.window.set_queue_memory(enabled):
            self.remember.blockSignals(True)
            self.remember.setChecked(self.window.queue_store.enabled)
            self.remember.blockSignals(False)
            self.status.setText("队列记录暂时无法保存或清除，请检查项目文件夹的写入权限；当前下载不受影响。")
            return
        self.refresh()

    def fill_url(self, row, _column):
        self.window.url.setText(self.window.queue_store.snapshot["items"][row]["url"])
        self.accept()

    def update_selection(self):
        self.remove.setEnabled(bool(self.table.selectionModel().selectedRows()))

    def remove_selected(self):
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        if rows and not self.window.remove_queue_items(rows):
            self.status.setText("没有移出任务：队列记录暂时不可写，或项目已变化。原记录仍保留，请稍后重试。")
            return
        self.refresh()

    def restore_clicked(self):
        if self.window.begin_queue_restore():
            self.accept()

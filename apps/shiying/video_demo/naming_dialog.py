"""Simple filename choices, with an editable name and preview for every row."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout
from .naming import filename_stem


class NamingDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("文件命名 · 只影响接下来的下载")
        self.resize(800, 510)
        layout = QVBoxLayout(self)
        self.mode = QComboBox()
        self.mode.addItem("标题 + 清晰度（例如：旅行记录_720p.mp4）", "title_quality")
        self.mode.addItem("仅标题（例如：旅行记录.mp4）", "title")
        self.mode.setCurrentIndex(self.mode.findData(window.naming_mode))
        layout.addWidget(self.mode)
        hint = QLabel("自定文件名可留空，留空时按上方规则命名；填写后使用该名称，并自动补上 .mp4。\n双击中间一列修改。非法字符会自动替换，重名加序号；不会改动已经保存的文件。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(len(window.videos), 3)
        self.table.setHorizontalHeaderLabels(["视频", "自定文件名（可选）", "保存名称预览"])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, video in enumerate(window.videos):
            title = QTableWidgetItem(video.title)
            title.setFlags(title.flags() & ~Qt.ItemFlag.ItemIsEditable)
            title.setToolTip(video.title)
            self.table.setItem(row, 0, title)
            self.table.setItem(row, 1, QTableWidgetItem(window.custom_names.get(row, "")))
            preview = QTableWidgetItem()
            preview.setFlags(preview.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 2, preview)
        layout.addWidget(self.table, 1)
        note = QLabel("清晰度后缀以实际保存的视频尺寸为准；预览中的最高画质仅作参考。开启队列记忆时会同时记住这些命名设置。")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox()
        buttons.addButton("应用", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.table.itemChanged.connect(self.refresh)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def refresh(self, *_):
        self.table.blockSignals(True)
        for row, video in enumerate(self.window.videos):
            height = self.window.table.cellWidget(row, 3).currentData() or max(video.heights, default=0)
            name = self.table.item(row, 1).text()
            preview = filename_stem(video.title, height, filename=name, naming=self.mode.currentData()) + ".mp4"
            self.table.item(row, 2).setText(preview)
            self.table.item(row, 2).setToolTip(preview)
        self.table.blockSignals(False)

    def names(self):
        return {row: self.table.item(row, 1).text().strip()[:300] for row in range(self.table.rowCount())
                if self.table.item(row, 1).text().strip()}

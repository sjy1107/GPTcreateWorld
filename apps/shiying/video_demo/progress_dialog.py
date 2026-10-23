"""Inspect and explicitly remove unfinished media in the current destination."""
from pathlib import Path
from datetime import datetime
import shutil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHeaderView, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout

from .resume import clear_progress, list_progress
from .transfer import bytes_label


class ProgressDialog(QDialog):
    def __init__(self, folder, parent=None):
        super().__init__(parent)
        self.folder = Path(folder).expanduser().resolve()
        self.setWindowTitle("未完成文件 · 查看与清理")
        self.resize(940, 470)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        hint = QLabel("这里只显示当前保存位置下的未完成素材。清理后需要重新下载，不会删除已完成的 MP4。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["选择", "视频", "占用空间", "进度状态", "最近使用时间"])
        self.table.horizontalHeaderItem(4).setToolTip('最近一次保留或更新这份素材的本机时间；不代表视频发布时间。')
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 52), (2, 105), (3, 175), (4, 155)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)
        self.selection_hint = QLabel('尚未选择要清理的素材。')
        layout.addWidget(self.selection_hint)
        self.table.itemChanged.connect(self.update_selection)
        row = QHBoxLayout()
        self.select_all = QCheckBox("全选")
        self.select_all.toggled.connect(self.toggle_all)
        row.addWidget(self.select_all)
        self.remove_button = QPushButton("清理选中进度")
        self.remove_button.clicked.connect(self.remove_selected)
        row.addWidget(self.remove_button)
        refresh = QPushButton("刷新列表")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        instructions = QLabel("要继续下载：关闭此窗口，重新查找原视频，选择相同画质和保存位置，并勾选“保留进度”。")
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.refresh()

    def refresh(self):
        try:
            self.records = list_progress(self.folder)
        except (OSError, ValueError, RuntimeError) as exc:
            self.records = []
            self.table.setRowCount(0)
            self.summary.setText("暂时无法读取进度：" + str(exc))
            self.selection_hint.setText('暂时无法读取素材；没有执行清理。')
            self.remove_button.setEnabled(False)
            return
        self.table.blockSignals(True)
        self.select_all.setChecked(False)
        self.table.setRowCount(len(self.records))
        for row, record in enumerate(self.records):
            choice = QTableWidgetItem()
            choice.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            if not record.get('can_clear', True):
                choice.setFlags(Qt.ItemFlag.NoItemFlags)
            choice.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, choice)
            quality = f"≤ {record['quality']}p" if record["quality"] else "最高 / 原画质"
            try:
                stamp = datetime.fromtimestamp(record['updated']).strftime('%Y-%m-%d %H:%M')
            except (ValueError, OverflowError, OSError, TypeError):
                stamp = '时间未知'
            for column, value in ((1, record["title"]), (2, bytes_label(record["bytes"])), (3, record["state"]), (4, stamp)):
                item = QTableWidgetItem(value)
                item.setToolTip(value + ("\n画质选择：" + quality if column == 1 else ""))
                self.table.setItem(row, column, item)
        total = sum(record["bytes"] for record in self.records)
        try:
            free = ' · 磁盘可用 ' + bytes_label(shutil.disk_usage(self.folder).free)
        except OSError:
            free = ' · 磁盘剩余空间暂不可读'
        self.summary.setText(f"保存位置：{self.folder}\n" + (f"{len(self.records)} 份素材，共占用 {bytes_label(total)}{free}。" if self.records else '没有保留的未完成文件' + free + '。'))
        self.table.blockSignals(False)
        self.update_selection()

    def toggle_all(self, checked):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def update_selection(self, *_):
        records = [record for row, record in enumerate(self.records)
                   if self.table.item(row, 0).checkState() == Qt.CheckState.Checked]
        self.remove_button.setEnabled(bool(records))
        self.selection_hint.setText(f"已选 {len(records)} 份，预计清理 {bytes_label(sum(record['bytes'] for record in records))}；这些任务将失去此处保存的素材或续传进度。" if records else '尚未选择要清理的素材。不会自动删除旧文件。')

    def remove_selected(self):
        keys = [record["key"] for row, record in enumerate(self.records)
                if self.table.item(row, 0).checkState() == Qt.CheckState.Checked]
        if not keys:
            QMessageBox.information(self, "先选择进度", "请先勾选要清理的项目。")
            return
        records = [record for record in self.records if record['key'] in keys]
        names = '\n'.join('• ' + record['title'][:100] for record in records[:5])
        if len(records) > 5:
            names += f'\n以及另外 {len(records) - 5} 项'
        message = f"将清理选中的 {len(keys)} 份素材，约 {bytes_label(sum(record['bytes'] for record in records))}：\n{names}\n\n这些任务将失去此处保存的素材或续传进度，需要重新下载。已完成的 MP4 会保留。"
        # Titles are untrusted text, never rich-text markup.
        dialog = QMessageBox(QMessageBox.Icon.Question, '清理未完成的素材？', message,
                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        try:
            answer = dialog.exec()
        finally:
            dialog.deleteLater()
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            clear_progress(self.folder, keys)
            self.refresh()
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "暂时无法清理", str(exc))

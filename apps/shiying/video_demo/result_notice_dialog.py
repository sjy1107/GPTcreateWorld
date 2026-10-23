"""Readable extraction facts, kept separate from download success/failure."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QLabel, QPushButton, QTextBrowser, QVBoxLayout


class ResultNoticeDialog(QDialog):
    def __init__(self, videos, row=0, parent=None):
        super().__init__(parent)
        self.videos = videos
        self.setWindowTitle('解析提示 · 找到地址不代表内容完整')
        self.resize(720, 530)
        layout = QVBoxLayout(self)
        intro = QLabel('这些说明来自本次网站返回的信息。下载成功和源视频是否完整，是两件需要分别确认的事。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.selector = QComboBox()
        for video in videos:
            self.selector.addItem(video.title)
        layout.addWidget(self.selector)
        self.content = QTextBrowser()
        self.content.setOpenLinks(False)
        layout.addWidget(self.content)
        close = QPushButton('知道了')
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.selector.currentIndexChanged.connect(self.refresh)
        self.selector.setCurrentIndex(max(0, min(row, len(videos) - 1)))
        self.refresh()

    def refresh(self, *_):
        if not self.videos:
            self.content.setPlainText('先查找视频，再查看提示。')
            return
        video = self.videos[self.selector.currentIndex()]
        parts = [video.title]
        for notice in video.notices:
            parts.append(notice.title + '\n' + notice.detail)
        if collection := video.info.get('_shiying_collection_notice'):
            parts.append('所属网页 / 列表的查找情况\n' + collection)
        parts.append('最终文件仍为 MP4；“已完成”表示已取得的媒体已经保存，不会自动补齐网站未提供的内容。')
        self.content.setPlainText('\n\n'.join(parts))

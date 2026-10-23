"""A small in-app walkthrough; browser actions happen only on explicit clicks."""
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout
from .help_ui import set_help_content

CHROMIUM_EXPORTER = "https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc"
FIREFOX_EXPORTER = "https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/"
COOKIE_FAQ = "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
EDGE_EXTENSIONS_HELP = "https://support.microsoft.com/en-gb/edge/add-turn-off-or-remove-extensions-in-microsoft-edge"


def downloads_folder() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    return Path(location) if location else Path.home() / "Downloads"


class CookieGuide(QDialog):
    def __init__(self, owner):
        super().__init__(owner.network_panel)
        self.owner = owner
        self.step = 0
        self.setWindowTitle("按步骤获取 Cookies · 无需寻找浏览器数据库")
        self.resize(750, 620)
        self.setMinimumSize(650, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        heading.addWidget(QLabel("你在哪个浏览器里登录？"))
        self.browser = QComboBox()
        for label, value in (("Edge", "edge"), ("Chrome", "chrome"), ("Firefox", "firefox"), ("Brave", "brave")):
            self.browser.addItem(label, value)
        self.browser.setCurrentIndex(max(0, self.browser.findData(owner.cookie_browser.currentData())))
        heading.addWidget(self.browser)
        heading.addStretch()
        self.counter = QLabel()
        heading.addWidget(self.counter)
        layout.addLayout(heading)
        self.content = QTextBrowser()
        self.content.setOpenExternalLinks(True)
        layout.addWidget(self.content, 1)
        self.actions = QHBoxLayout()
        layout.addLayout(self.actions)
        self.copy_video = self.action("复制视频网址", self.copy_target)
        self.open_extension = self.action("打开扩展商店页面", self.open_store)
        self.copy_extension = self.action("复制商店链接", self.copy_store)
        self.open_downloads = self.action("打开“下载”文件夹", self.show_downloads)
        self.import_clipboard = self.action("从剪贴板导入", self.paste_export)
        self.import_file = self.action("选择刚导出的文件", self.choose_export)
        self.message = QLabel("")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setMinimumHeight(42)
        layout.addWidget(self.message)
        navigation = QHBoxLayout()
        self.previous = QPushButton("上一步")
        self.previous.clicked.connect(lambda: self.move(-1))
        self.next = QPushButton("下一步")
        self.next.setObjectName("primary")
        self.next.clicked.connect(lambda: self.move(1))
        self.done_button = QPushButton("关闭指引")
        self.done_button.clicked.connect(self.accept)
        navigation.addWidget(self.previous)
        navigation.addWidget(self.next)
        navigation.addStretch()
        navigation.addWidget(self.done_button)
        layout.addLayout(navigation)
        self.browser.currentIndexChanged.connect(lambda: self.render())
        self.render()

    def action(self, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        self.actions.addWidget(button)
        return button

    @property
    def store_url(self):
        return FIREFOX_EXPORTER if self.browser.currentData() == "firefox" else CHROMIUM_EXPORTER

    def move(self, amount):
        self.step = min(3, max(0, self.step + amount))
        self.message.clear()
        self.render()

    def render(self):
        browser = self.browser.currentText()
        firefox = self.browser.currentData() == "firefox"
        install = ("扩展名称是 **cookies.txt**，商店显示的作者为 **Lennon Hill**。点击“添加到 Firefox”，查看权限提示后确认。"
                   if firefox else "扩展名称是 **Get cookies.txt LOCALLY**，商店显示的提供者为 **kairi003**。核对完整名称后，点击“获取 / 添加”，查看权限提示并确认。")
        edge_note = ("\n\nEdge 若提示“允许来自其他商店的扩展”，选择允许后再添加。它只是允许从 Chrome 商店安装这个工具；公司管理的浏览器可能不允许安装。"
                     if self.browser.currentData() == "edge" else "")
        export = ("点击地址栏右侧的**扩展 / 拼图图标**，再点 **cookies.txt**。选择只导出**当前网站**的选项；不要导出全部网站。扩展导出的文本应为 Netscape 格式。若有“复制”选项，也可复制当前网站内容。"
                  if firefox else "点击地址栏右侧的**扩展 / 拼图图标**，再点 **Get cookies.txt LOCALLY**。\n\n1. 顶部网站名称应与视频网站一致。\n2. 找到 **Export Format（导出格式）**，建议选择 **Netscape**；已导出的常见 JSON 列表也可直接导入，无需手动转换。\n3. 最省事：点 **Copy（复制）**，然后到下一步点“从剪贴板导入”。\n4. 或点 **Export（导出）** 保存文件；Export As（另存为）可自己选保存位置。\n\n不要选 Header String 或 **Export All Cookies（导出所有网站）**。")
        pages = [
            f"## 1. 在 {browser} 中确认视频能播放\n先回到你平时使用的 **{browser}**，用自己的账号登录目标网站，打开**具体视频页面**并确认能够播放。\n\n软件主界面也要粘贴这个视频的网址；下方按钮可复制它，方便粘到浏览器地址栏。复制网址本身不会复制登录状态。\n\n**普通公开视频无需做这些设置，直接查找即可。**\n\n软件已有“读取浏览器”功能，读取成功时无需导出文件。这个指引用于读取失败或希望手动导入的情况。",
            f"## 2. 准备导出工具（通常只做一次）\n{install}{edge_note}\n\n商店页面会在系统默认浏览器打开。如果打开的不是 **{browser}**，点“复制商店链接”，粘贴到 **{browser}** 的地址栏中。扩展需要安装在已登录网站的那个浏览器用户里。\n\n已经安装过？直接点下一步。商店打不开时可使用网络允许访问的浏览器，或回到软件尝试直接读取；本软件不提供来历不明的安装包。\n\n[工具来源：yt-dlp 官方推荐]({COOKIE_FAQ})" + (f" · [Edge 安装说明]({EDGE_EXTENSIONS_HELP})" if self.browser.currentData() == "edge" else ""),
            f"## 3. 在视频页面复制或导出\n回到已经登录的**视频页面**，不要停留在扩展商店。\n\n{export}\n\n找不到扩展？点浏览器右上角的拼图图标或“⋯ → 扩展”。如果扩展提示没有网站访问权限，在浏览器中允许它访问当前网站，然后再打开扩展。\n\n不用打开 F12，也不用寻找 AppData 下的 Cookies 数据库。",
            f"## 4. 回到软件导入\n**复制导入（免找文件）**\n\n刚在扩展里点了 Copy？现在点下方 **从剪贴板导入**。支持 Netscape 文本和常见 JSON 导出列表。\n\n**文件导入**\n\n点 **选择刚导出的文件**，选刚保存的 .txt 或 .json。通常在：\n\n`{downloads_folder()}`\n\n找不到时，在 {browser} 按 **Ctrl+J**，在刚导出的记录旁点文件夹图标或“在文件夹中显示”。Export As 可以自选保存位置。不要选浏览器内部数据库。\n\n**导入成功之后**\n\n关闭指引 → 可点 **检查导入信息** 核对网站与过期情况 → 点 **完成并查找视频** 重新查找主界面的网址。不会自动下载，网站仍会检查账号权限。\n\n只在你点击导入时读取剪贴板；不会自动监控。Cookies 是登录凭据，请勿分享。",
        ]
        self.counter.setText(f"{self.step + 1} / 4")
        set_help_content(self.content, pages[self.step])
        self.previous.setEnabled(self.step > 0)
        self.next.setVisible(self.step < 3)
        for button, step in ((self.copy_video, 0), (self.open_extension, 1), (self.copy_extension, 1),
                             (self.open_downloads, 3), (self.import_clipboard, 3), (self.import_file, 3)):
            button.setVisible(self.step == step)
        self.import_clipboard.setToolTip("先在扩展中点 Copy；支持 Netscape 文本或常见 JSON 列表。只在点击此按钮时读取剪贴板。")

    def copy_target(self):
        from .core import validate_url
        try:
            url = validate_url(self.owner.url.text())
        except ValueError:
            self.message.setText("主界面还没有有效视频网址，请先关闭指引并粘贴网址，再回来继续。")
            return
        QApplication.clipboard().setText(url)
        self.message.setText("网址已复制，请粘贴到已登录的浏览器地址栏。")

    def open_store(self):
        if not QDesktopServices.openUrl(QUrl(self.store_url)):
            self.message.setText("未能打开浏览器，请点“复制商店链接”，手动粘贴到浏览器中。")
        else:
            self.message.setText("已请求默认浏览器打开商店。请确认使用的是上面选择的浏览器和用户。")

    def copy_store(self):
        QApplication.clipboard().setText(self.store_url)
        self.message.setText("商店链接已复制，请粘贴到已登录网站的那个浏览器中。")

    def show_downloads(self):
        folder = downloads_folder()
        if not folder.is_dir() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self.message.setText("没有打开下载文件夹。请在浏览器按 Ctrl+J，在刚导出的文件旁点“在文件夹中显示”。")

    def paste_export(self):
        try:
            self.owner.accept_cookie_text(QApplication.clipboard().text())
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        self.imported()

    def choose_export(self):
        if self.owner.browse_cookies(parent=self):
            self.imported()

    def imported(self):
        self.message.setText("已导入当前网站可用的 Cookies。关闭指引后，可检查导入信息，再点“完成并查找视频”重新查找；网站仍需确认账号权限。")
        self.done_button.setText("已导入，返回设置")

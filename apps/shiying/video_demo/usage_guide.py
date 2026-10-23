"""Searchable task guidance. Opening it never reads accounts or starts a task."""
import re
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QTextBrowser, QVBoxLayout)
from .help_ui import QUICK_HELP, LOGIN_HELP, set_help_content


@dataclass(frozen=True)
class Topic:
    key: str
    title: str
    keywords: str
    content: str
    action: str = ''
    action_label: str = ''


def guide_topics():
    sections = {}
    for section in re.split(r'(?m)^## ', QUICK_HELP):
        title, _, body = section.partition('\n')
        if title.strip():
            sections[title.strip()] = body.strip()
    definitions = [
        ('first', '第一次用：保存一个视频', '三步 开始 mp4 网址 链接 粘贴 播放', '三步保存视频', 'source', '回到网址输入框'),
        ('batch', '一次下载多个链接', '批量 分享文字 多个 全选 去重', '一次添加多个链接', 'batch', '打开批量链接窗口'),
        ('quality', '选择清晰度、查看速度', '高清 720 1080 4K 画质 大小 进度 百分比 限速', '画质与进度', 'quality', '查看视频与清晰度'),
        ('storage', '选择保存位置、检查空间', '文件夹 磁盘 存储 U盘 FAT32 路径 4GB', '保存盘空间', 'storage', '回到保存位置'),
        ('naming', '修改视频文件名', '命名 重命名 重名 标题 名称 后缀', '文件怎么命名', 'naming', '打开文件命名'),
        ('control', '跳过一个，或取消整批', '停止 暂停 跳过 取消 卡住', '跳过当前视频或取消全部', 'controls', '回到任务进度'),
        ('failure', '下载失败了怎么处理', '错误 失败 403 404 429 超时 网络 诊断 重试', '失败了怎么办', 'failure', '打开失败原因'),
        ('resume', '中断后怎样继续下载', '断点 续传 保留进度 整理 关闭 重开', '保留进度与继续下载', 'controls', '回到任务进度'),
        ('queue', '重启后找回未完成任务', '队列 恢复 重启 记住 记忆 移出', '重启后恢复任务列表', 'queue', '打开任务队列'),
        ('login', '网页需要登录怎么办', 'cookie cookies 账号 登录 年龄 权限 txt json 导入 复制', '网站需要登录时', 'login', '打开登录信息设置'),
        ('history', '找回视频、修复文件位置', '历史 重复 视频移动 重新定位 关联 搜索 丢失 失效', '下载历史与重复提醒', 'history', '打开下载历史'),
    ]
    topics = [Topic(key, title, words, sections[section], action, label)
              for key, title, words, section, action, label in definitions]
    topics.insert(4, Topic('cleanup', '清理未完成素材的占用', '缓存 未完成 空间 清理 删除 最近使用时间',
        '1. 在主界面确认当前的**保存位置**，点 **刷新占用** 查看保留素材占了多少空间。\n'
        '2. 点底部 **未完成文件…**，查看各任务的大小和最近使用时间。\n'
        '3. 只勾选不准备继续下载的任务，点击 **清理选中进度**。\n'
        '4. 确认任务名称与占用后再决定；默认是取消。\n\n'
        '**清理素材与移除历史记录不同。** 清理素材会失去这些任务的续传内容，之后要重新下载；'
        '已完成的 MP4 不会被清理。移除历史只删除记录，不会腾出视频文件占用的空间。\n\n'
        '记录不完整的旧素材会计入占用，但不提供直接清理。软件不会按时间自动删除文件。',
        'partials', '打开未完成文件'))
    topics.append(Topic('edge', 'Edge / Chrome 读取失败', 'edge chrome 浏览器 占用 解密 加密 用户 资料 导出 扩展',
        '先确认主界面已经填入目标网站的**视频网页网址**。读取的是这个网址所属网站的登录信息，'
        '不是浏览器当前打开的标签页。\n\n' + LOGIN_HELP,
        'login', '打开登录与导入设置'))
    topics.append(Topic('proxy', '什么时候需要填写代理', '代理 proxy 连接 网络 vpn 端口 地址',
        '多数情况下先**留空**，沿用系统设置。代理不是登录，也不会增加账号的观看权限。\n\n'
        '如果你已经在本机使用代理程序，并知道其 HTTP 代理端口：\n'
        '1. 打开 **登录与代理**，展开 **代理设置**。\n'
        '2. 输入完整地址，例如 `http://127.0.0.1:7890`。7890 只是例子，请填写自己的实际端口。\n'
        '3. 确认代理程序正在运行，再回到主界面重试原链接。\n\n'
        '不清楚端口时不要猜填。可以清空输入框恢复系统设置；修改设置本身不会自动下载。',
        'proxy', '打开代理设置'))
    topics.append(Topic('terms', '常用词是什么意思', '术语 不懂 入门 新手 MP4 Cookies HLS DASH 转码 封装',
        '- **视频网页网址**：浏览器地址栏中的播放页链接。应优先复制这个地址。\n'
        '- **视频直链**：直接指向媒体文件的地址，可能很快过期；不必自己找这种地址。\n'
        '- **清晰度 / 720p、1080p**：视频画面的尺寸档位。只能选网站实际提供的画质，不会把低清变高清。\n'
        '- **MP4**：本软件最终保存的文件格式；不是清晰度等级。\n'
        '- **整理 / 合并**：把画面、声音整理成一个文件；下载进度到 100% 后有时还要等这一步。\n'
        '- **转码**：把不易播放的视频编码转换为更常用的编码，需要时间，也可能产生少量画质损失。\n'
        '- **HLS / DASH**：网站把视频分段或把画面与声音分开传输的方式，软件会尝试处理，无需你手动拼接。\n'
        '- **Cookies**：网站保存在浏览器里的访问或登录凭据。只有网站要求时再添加自己的凭据，不要发给别人。\n'
        '- **任务队列**：等待下载或需要重试的视频列表。记住任务与保留下载素材是两件事。\n'
        '- **断点续传**：中断后从可靠的进度继续，部分网站和分片视频仍需重新下载。'))
    topics.append(Topic('limits', '为什么有些网页找不到视频', '支持 网站 全部 直播 私密 vip 会员 drm 无法识别',
        '软件会尝试识别当前网页公开给下载工具的资源，**不保证找出任意网页的全部视频**。\n\n'
        '除了具体视频的播放页，也会补充查找网页里的嵌入播放器、延迟加载地址、视频清单和常见播放器配置。'
        '网页里有多个独立视频时会尽量分别列出，同一视频的不同画质放在一起。最终仍只保存 MP4。\n\n'
        '这里只读取网页已有的数据，不会代替你点击播放、执行整页脚本或完成网站验证。'
        '如果只找到部分视频，可以复制遗漏视频自己的播放页链接再查找。\n\n'
        '先按顺序检查：\n'
        '1. 复制具体视频的播放页，避免网站首页、个人主页或整个频道。\n'
        '2. 在同一网络下，确认自己的浏览器能播放这个具体视频。\n'
        '3. 网页要求登录时，再按登录指南添加自己的信息；普通公开视频先直接尝试。\n'
        '4. 查看失败原因，区分网络错误、链接失效、观看权限和网站兼容问题。\n\n'
        '当前不处理正在直播的流，不遍历无限滚动页面或私聊；每批最多 20 个链接、50 个视频。'
        '登录信息不会增加会员或购买权限，软件不解密 DRM。网站改版后，浏览器能播放也可能暂时无法下载。',
        'source', '回到网址输入框'))
    topics.extend([
        Topic('notices', '解析成功后，怎样确认内容', '试看 预览 完整 内容 缺失 画质 提示 列表 部分',
              '找到地址和保存成功，不能证明源视频完整。列表的 **解析提示** 与下载 **状态** 分开显示。\n\n'
              '1. 点列表上方 **解析提示…**，或双击某一行的解析提示，查看该视频的说明。\n'
              '2. **可能只有试看内容**：网站明确提示了预览 / 试看，保存为 MP4 也不会自动变成完整版。\n'
              '3. **部分画质未取得**：只列出本次取得的画质，可能与权限或解析兼容有关。\n'
              '4. **完整性未确认**：没有足够信息证明内容完整，不表示视频必然有问题；需要时与原网页的时长和内容对照。\n'
              '5. **所属网页 / 列表**的提示说明其他条目或补充检查未完成，不代表当前这个视频损坏。\n\n'
              '使用自己的合法观看权限获取内容；下载结果仍只保存 MP4。', 'notices', '查看当前解析提示'),
        Topic('engine', '解析引擎更新与恢复', '更新 升级 引擎 yt-dlp 版本 回退 恢复 稳定版 预发布',
              '网站改版时，更新解析引擎可能有帮助，但不能保证某个网站一定恢复。\n\n'
              '1. 先结束当前任务，打开 **检查与数据 → 解析引擎更新**。\n'
              '2. 选稳定版或预发布版，点 **检查官方更新**；只有点击才联网，不读取账号。较新的预发布版可能比稳定版新，不自动降级。\n'
              '3. 有更新时，点 **下载、验证并安排下次启用**。新版在独立目录校验，并使用本机测试视频检查 MP4 输出；附加组件不满足要求时保留原版本。\n'
              '4. 成功后关闭并重新打开软件才启用。未重启前可取消安排；启用后可安排下次启动恢复之前的引擎。\n'
              '5. 新版未通过启动检查时会恢复之前的版本；提示需要重新打开时，请再从 start.bat 启动。\n\n'
              '原环境、视频和 Cookies 不因更新被清除。需要保留任务列表时，关闭前先开启“记住队列”。\n\n'
              '免安装版已自带运行组件。若新版引擎需要新的附加组件，请下载新版完整免安装包，不需要自己安装 Python 或执行命令。', 'engine', '打开解析引擎管理'),
        Topic('health', '启动与环境检查', '启动 环境 组件 缺失 Python FFmpeg Node 错误 自检',
              '启动时会在后台检查本机组件，右上角 **检查与数据** 可随时查看结果。\n\n'
              '1. 打开 **检查与数据 → 环境检查**，选中一项查看说明。\n'
              '2. **需处理**表示关键组件有问题；例如 FFmpeg 无法运行时会暂时禁止查找和下载，避免重复失败。\n'
              '3. **需注意**可能只是部分网站需要的可选组件缺失，普通视频仍可尝试。\n'
              '4. 按说明修复或更改保存位置后，点击 **重新检查**。检查不会自动安装组件、联网测网站或读取账号。\n\n'
              '界面无法启动时，请从 start.bat 打开，查看启动提示和本机启动记录。不要通过清除视频来修复运行环境。',
              'health', '打开环境检查'),
        Topic('data', '本机数据放在哪里', '数据 设置 记录 存储 登录 日志 隐私 清空 占用',
              '打开 **检查与数据 → 本机数据**，选中一类数据查看位置、占用和处理说明。\n\n'
              '- **保存位置记录**：清除后本次目录不变，下次使用默认目录；之后选择目录或下载会重新记住。\n'
              '- **下载历史**：清空只删除记录，视频保留，数据库体积不一定马上缩小。\n'
              '- **任务队列**：清除并关闭记忆；主列表、成品视频和续传素材保留。\n'
              '- **未完成素材**：点管理进入逐项清理；确认后会失去对应的续传内容。\n'
              '- **MP4 文件**：此页只统计当前目录第一层，不遍历子文件夹，也不提供删除。\n'
              '- **登录信息**：清除软件的暂存会话和文件选择，不退出浏览器、不删除导出文件。\n'
              '- **运行记录**在内存中；**启动失败记录**是单独的一份本机文件，成功启动后旧记录仍可能存在。\n\n'
              '- **解析引擎版本**：原环境和独立版本分开存放。此页不直接删除，点管理进入更新 / 恢复页。\n\n'
              '统计在后台按需进行，只针对明确的位置，不扫描全盘。清除前请阅读影响范围，默认取消。',
              'data', '打开本机数据'),
    ])
    from .paths import frozen
    if frozen():
        topics.append(Topic('portable', '免安装版：移动、升级与版本', '免安装 解压 升级 备份 data 版本 文件夹',
                            '当前为 **拾影 0.1.0 Windows 免安装版**。\n\n'
                            '1. 将 ZIP **完整解压**到自己的可写文件夹，再双击 **拾影.exe**；不要只移动 exe。\n'
                            '2. **data** 保存本机设置、历史、可选队列及引擎记录；**downloads** 是默认视频目录。\n'
                            '3. 移动软件时请移动整个文件夹；在软件内部的保存路径会跟随移动，外部磁盘路径仍指向原位置。\n'
                            '4. 升级整包前关闭软件并备份 data，将新版本解压到新文件夹后复制 data；用户自选位置的视频无需搬移。\n'
                            '5. 关闭软件后可复制整个文件夹备份。不要将自己的 data、视频或 Cookies 重新打包分享给别人。\n\n'
                            '免安装版已包含运行组件，不需要安装 Python。应用版本与解析引擎版本分别显示。'))
    return tuple(topics)


class UsageGuide(QDialog):
    def __init__(self, parent=None, *, topic='first', availability=None):
        super().__init__(parent)
        self.action = ''
        self.topics = guide_topics()
        self.availability = availability or {}
        self.setWindowTitle('使用指南 · 按你想做的事查找')
        self.resize(960, 640)
        self.setMinimumSize(760, 460)
        layout = QVBoxLayout(self)
        intro = QLabel('选一个问题查看步骤，也可搜索“登录”“找不到文件”“空间”等关键词。看完后可直接打开对应功能。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索操作或不懂的词，例如 Cookies、续传、文件名')
        self.search.setAccessibleName('搜索使用指南')
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.setAccessibleName('使用指南主题')
        self.list.setMinimumWidth(235)
        self.list.setMaximumWidth(290)
        self.list.setSpacing(3)
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setStyleSheet('QListWidget {background: white; border: 1px solid #dce3ef; border-radius: 8px; padding: 4px;} '
                                'QListWidget::item {padding: 8px 5px; border-radius: 5px;} '
                                'QListWidget::item:selected {background: #e9efff; color: #234fba;}')
        row.addWidget(self.list)
        self.content = QTextBrowser()
        self.content.setAccessibleName('操作步骤说明')
        self.content.setOpenLinks(False)
        self.content.setOpenExternalLinks(False)
        row.addWidget(self.content, 1)
        layout.addLayout(row, 1)
        self.action_hint = QLabel('')
        self.action_hint.setWordWrap(True)
        layout.addWidget(self.action_hint)
        footer = QHBoxLayout()
        self.go = QPushButton('打开对应功能')
        self.go.setObjectName('primary')
        self.go.setAutoDefault(False)
        self.go.clicked.connect(self.choose_action)
        footer.addWidget(self.go)
        footer.addStretch()
        close = QPushButton('关闭指南')
        close.setAutoDefault(False)
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.search.textChanged.connect(self.filter_topics)
        self.list.currentItemChanged.connect(self.display_topic)
        self.search.returnPressed.connect(self.content.setFocus)
        self.filter_topics()
        self.select_topic(topic)

    def select_topic(self, key):
        for row in range(self.list.count()):
            if self.list.item(row).data(Qt.ItemDataRole.UserRole).key == key:
                self.list.setCurrentRow(row)
                return

    def filter_topics(self, *_):
        previous = self.list.currentItem()
        key = previous.data(Qt.ItemDataRole.UserRole).key if previous else ''
        words = self.search.text().casefold().split()
        self.list.blockSignals(True)
        self.list.clear()
        for topic in self.topics:
            text = (topic.title + ' ' + topic.keywords + ' ' + topic.content).casefold()
            if all(word in text for word in words):
                item = QListWidgetItem(topic.title)
                item.setData(Qt.ItemDataRole.UserRole, topic)
                item.setToolTip(topic.title)
                self.list.addItem(item)
        self.list.blockSignals(False)
        self.list.setCurrentRow(0 if self.list.count() else -1)
        self.select_topic(key)
        self.display_topic()

    def display_topic(self, *_):
        item = self.list.currentItem()
        if not item:
            set_help_content(self.content, '## 没有找到相关说明\n试试更短的关键词，例如“登录”“空间”或“历史”，也可清空搜索查看全部功能。')
            self.go.setEnabled(False)
            self.go.setText('没有匹配的功能')
            self.action_hint.setText('只在应用内搜索帮助，不联网。')
            return
        topic = item.data(Qt.ItemDataRole.UserRole)
        set_help_content(self.content, '## ' + topic.title + '\n' + topic.content)
        availability = self.availability() if callable(self.availability) else self.availability
        reason = availability.get(topic.action, '')
        self.go.setText(topic.action_label or '本页为说明，无需操作')
        self.go.setEnabled(bool(topic.action) and not reason)
        self.action_hint.setText(reason or ('只打开对应窗口或定位到控件，不会自动查找、下载、导入登录信息或清理文件。' if topic.action else '本页解释常用词；不需要改变任何设置。'))

    def choose_action(self):
        self.display_topic()  # Recheck state before leaving the guide.
        item = self.list.currentItem()
        if item and self.go.isEnabled():
            self.action = item.data(Qt.ItemDataRole.UserRole).action
            self.accept()

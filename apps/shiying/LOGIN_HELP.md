# Instagram / X 等登录资源的使用方法

本版支持导入自己的登录会话，不在软件中保存账号密码。**先确认自己的账号在浏览器中能正常播放这个具体视频。** 若账号仍需完成年龄验证、没有私密内容权限或受地区限制，请先在网站自身处理；导入 Cookies 不会增加账号权限。

**只复制视频网页链接，通常不会复制登录状态。** 浏览器在请求时会另外发送 Cookies，软件仅拿到链接时通常仍是未登录状态。分享参数一般不是登录凭据；少数带临时签名的媒体直链属于另一种情况，也可能过期或绑定请求条件。

## 公开链接：直接解析

普通网页、公开视频和直接 MP4 / M3U8 / MPD 等资源，粘贴网址后直接解析、选画质、下载即可。默认没有登录信息，也不会自动访问浏览器。先前读取过会话时，可点击「清除登录信息」同时清除内存会话和文件选择；不删除用户原文件。

换到其他网站时，不会把已读取会话用于新网站。若内存会话已全部过期，本次先尝试公开资源；遇到确实需要登录的页面，再重新读取。

已经添加了登录信息，但怀疑它失效或导出文件已移动？在「登录与代理」中取消勾选 **使用已添加的登录信息**，本轮查找和下载便按公开访问尝试，不发送导入的 Cookies。原设置保留，重新勾选即可恢复；新导入或读取成功会重新启用。部分访问错误窗口也有「按公开链接重试」。这仅适用于本来就能公开访问的视频，不会增加观看权限。

## 快速方式：从浏览器读取

1. 在自己的浏览器中登录并确认目标视频能播放，再把视频网址粘贴到软件。
2. 点击「登录与代理…」，选择 **Firefox / Edge / Chrome / Brave**，点击「读取当前网站登录状态」。这里的“当前网站”指软件内粘贴的网址，不会自动查看浏览器当前标签页。
3. 默认会自动查找资料。有多个资料时，可在可选输入框填写资料名或目录：Chrome / Edge 常见资料名为 `Default`、`Profile 1`；Firefox 可填资料文件夹的完整路径。
4. 读取成功后会显示网站和 Cookie 数量，再点击「查找视频」。读取成功不代表已确认账号登录或有权观看。
5. 「清除登录信息」或关闭软件会释放保留的内存会话。需要更新会话时，再主动读取一次。

浏览器确实把 Cookies 保存在本机资料目录的数据库中，例如 Chrome / Edge 的资料下 `Network/Cookies`，Firefox 的资料下 `cookies.sqlite`；部分内容会被浏览器或操作系统加密。本版复用 yt-dlp 的读取实现，不需要手动寻找或复制数据库。

**读取范围与保存方式**：yt-dlp 的读取器会处理所选资料中的 Cookie 集合，并短暂复制完整数据库到本机临时目录，结束后清理。本软件随后按网站筛选，只保留当前网站尚未过期的 Cookies，供解析/下载使用；不把无关网站的 Cookie 集合写入任务文件。筛选结果暂存在内存中，每个任务写入独立临时文件，用后清理。不会修改浏览器原资料，不记录 Cookie 名称和值，不自动上传会话文件。访问目标网站时会按正常的域名和路径规则发送 Cookies。

X 的 `x.com` / `twitter.com` 等已知站点别名按同一网站处理；对未识别网站仅按输入主机名（去除 `www.`）筛选，不猜测公共后缀。如果复杂登录依赖其他域名，可以改用自行导出的文件。

## 备用方式：导出当前网站，再导入软件

1. 在自己的浏览器中登录 Instagram 或 X，打开待下载视频并确认能播放。
2. 用可信的 Cookies 导出工具，仅导出**当前网站**的 Cookies，建议保存为 **Mozilla/Netscape 格式的 `.txt` 文件**。也支持常见 JSON Cookies 记录列表，以及包含 `cookies` 列表的对象；浏览器数据库、直接复制请求头、只有名称和值的 JSON 对象及分区 Cookies 不支持。也可以复制导出内容后在软件中主动从剪贴板导入。
3. 打开 Demo，点击「登录与代理…」，点击「选择 Cookies 文件」。
4. 选择导出的文件后，可点击「检查导入信息」核对当前网站、可用条数和过期情况，不连接网站，也不显示 Cookies 内容。再点「完成并查找视频」，重新读取主界面的网址；成功后替换结果，不自动下载。之后选择清晰度、保存为 MP4。仅导入文件不会自动更新此前解析得到的结果。
5. 提示会话失效时，在浏览器中重新登录、确认播放、重新导出并解析。检查浏览器和软件是否使用可访问该网站的网络。

yt-dlp 官方 FAQ 列出了 Chrome 的 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) 和 Firefox 的 [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/) 扩展。安装前核对名称、来源与权限；导出时选择当前网站，避免导出全部站点。普通 `Get cookies.txt` 与带 **LOCALLY** 的扩展不同，官方 FAQ 曾明确警告前者存在恶意软件报告。

Cookies 相当于登录凭据，不要发到聊天、上传在线下载站或提交代码仓库。软件只在本机读取，并使用临时副本，任务结束后清理副本；原文件由你自行保管或删除。项目已忽略文件名带 `cookies` 的 `.txt`，其他命名的文件也不要提交。

## Chrome / Edge 读取失败怎么办

Windows 上 Chromium 系浏览器的 Cookie 加密、浏览器占用等情况可能使外部读取失败。如果提示占用，可正常退出所选浏览器后再试；如果提示无法解密，可改用 Firefox 中自己的登录会话，或采用上述文件导入方式。不需要关闭浏览器安全功能、以管理员运行软件或修改系统策略。

如果只读取到部分 Cookies，会显示登录可能不完整的提示。找不到当前网站 Cookies 时，请确认选择的是实际登录过的浏览器资料，并先打开视频。点击读取前不会自动访问任何浏览器资料。

## GitHub 项目比较与本版选择

| 项目 | 可用能力 | 本版选择 |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 视频解析、画质、下载，支持 Cookies 文件与浏览器会话；Instagram / X 解析器识别账号会话 | 已使用；复用浏览器读取功能，增加网站筛选和临时会话管理 |
| [gallery-dl](https://github.com/mikf/gallery-dl) | Instagram / X 的帖子、图片视频合集及 Cookies 认证 | 可作后续针对性备选，暂不额外安装或接入 |
| [Instaloader](https://github.com/instaloader/instaloader) | Instagram 专用下载，支持登录会话及从浏览器导入会话 | 仅 Instagram 的备选，不适用于 X，暂不接入 |

备选工具同样依赖有效会话和可用的站点接口，不能保证解决所有限制。先用一套解析器与用户自己的有效会话确认具体问题，有明确失败证据后再考虑增加依赖，保持初版简单。

## 当前实测状态

2026-10-17，用户提供的 Instagram Reel 在未登录请求下返回 `This content isn't available to everyone: It can't be seen by certain audiences.`。已明确识别为受众访问限制，**未完成登录后的真实下载验证**。X 先前仅验证过公开帖子的解析，未验证需要登录的帖子。

新增读取功能通过合成 Firefox 数据库的真实上游读取，以及界面后台线程的“读取 → 解析 → 清理”验证。没有读取用户的真实浏览器资料，没有验证真实 Chrome / Edge 加密 Cookie 或真实账号登录资源；需要用户自行选择已登录的浏览器实际体验。

## 官方参考（2026-10-17 查阅）

- [yt-dlp：传入 Cookies](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)
- [yt-dlp：Windows Chrome Cookie 解密问题 #10927](https://github.com/yt-dlp/yt-dlp/issues/10927)
- [yt-dlp 的 X / Twitter 解析器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/twitter.py)
- [gallery-dl：Cookies 认证](https://github.com/mikf/gallery-dl#cookies) 与 [支持站点](https://github.com/mikf/gallery-dl/blob/master/docs/supportedsites.md)
- [Instaloader：登录及浏览器会话](https://instaloader.github.io/cli-options.html#login-download-private-profiles)

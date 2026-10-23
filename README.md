# GPTcreateWorld · 作品集

这里收录个人开发的实用工具与应用。每个作品独立存放，附有简体中文介绍、安装步骤和使用说明。

## 当前作品

| 作品 | 用途 | 技术与平台 | 状态 |
| --- | --- | --- | --- |
| [拾影 · 视频下载器](apps/shiying/) | 粘贴视频网页地址，选择视频和清晰度，保存为本地 MP4 | Python、PySide6、yt-dlp、FFmpeg；Windows x64 | 0.1.0 初期可用版 · 未签名 |

### 拾影能做什么？

查找网页中可识别的视频，按需选择清晰度和保存位置，显示下载进度、速度和剩余时间；提供失败重试、下载历史、任务恢复、登录信息导入、环境检查及解析引擎更新。

**直接使用：[前往 Releases 下载 Windows 免安装版](https://github.com/sjy1107/GPTcreateWorld/releases)。** 下载名称包含 `Windows-x64-Portable.zip` 的附件，完整解压，进入 `Shiying` 文件夹后双击“拾影.exe”，无需安装 Python。

**查看源码：[apps/shiying](apps/shiying/)。** 开发者可按[源码运行说明](apps/shiying/README.md)安装依赖。GitHub 的 **Code → Download ZIP** 和 Release 自动附带的 **Source code** 都是源码，不能直接双击运行。

目前没有 Windows 代码签名证书，程序未签名，部分电脑可能提示“未知发布者”或阻止运行。具体说明和操作步骤见[下载与启动说明](apps/shiying/DOWNLOAD.md)。

![拾影界面示例](apps/shiying/docs/ui-result-notices.png)

## 仓库结构

```text
apps/
  shiying/       拾影视频下载器：源码、测试、说明与示例界面
GitHub Releases  按版本提供免安装 ZIP、源码构建材料及 SHA-256；成品不提交到源码目录
```

后续作品可继续添加到 `apps/` 下的独立目录，使用各自的依赖环境，避免相互影响。

仓库仅包含项目源码、合成素材测试程序和示例界面，不包含下载视频、浏览器登录凭据、本机历史或 Python 虚拟环境。

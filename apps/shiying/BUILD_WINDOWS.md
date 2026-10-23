# Windows 免安装版 0.1.0

本轮打包工作全部位于 `C:\vsProject\ShiyingPortableBuild`，没有在其他目录创建构建环境或产物。

Windows 成品通过 [GitHub Releases](https://github.com/sjy1107/GPTcreateWorld/releases) 分发，选择名称包含 `Windows-x64-Portable.zip` 的附件。完整解压后打开 Shiying 文件夹，双击“拾影.exe”，无需安装 Python、FFmpeg 或开发依赖。请保留整个文件夹。

应用源码位于仓库 `apps/shiying`；本版完整构建材料另作为 Release 的 `Source-and-Build.zip` 附件提供，其中包含打包脚本、依赖版本、第三方组件获取记录和构建说明。重新构建前按其中的 BUILD_README.md 准备环境。默认构建不生成压缩包，并在独立目录完成成品验收后再替换，旧版和用户数据保留在 backups。发布归档从独立、干净的运行组件副本生成，不会复制用户 data 或 downloads。

应用版本设为 0.1.0，内置解析引擎另行显示版本。包内包含 Python、Qt、FFmpeg / ffprobe、Deno、网站解析组件及必要的依赖元数据。记录写入程序旁的 data；内部保存路径可跟随整个文件夹移动，外部目录仍指向原位置。

296 项源码测试与 5 项更新保留数据测试通过。已修复打包误带入不兼容 ICU DLL 导致的启动失败，真实冻结 EXE 在隔离 PATH、独立中文目录下完成正常启动、MP4 / WebM / HLS / DASH 四种本地来源下载和 MP4 完整解码，后台引擎 worker 的完整 MP4 验证通过。程序的 40 个源码模块与冻结包编译代码逐个对照一致，替换后原位置 EXE 启动也通过。

本版未签名，部分电脑仍可能拦截。原始系统拦截记录为早期历史；本轮真实冻结 EXE 验收通过，没有改变系统安全设置。另一台电脑 / Windows 10 实机尚未验收。

本机打包不自动上传 GitHub Releases。公开发布前请附未签名说明、SHA-256、第三方许可及按许可需要的源码材料。组件来源索引不等于已经收集所有第三方完整源码。

@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 拾影尚未找到本项目的 Python 环境，程序没有启动。
  echo 请先安装 Python 3.12，并在当前项目文件夹的终端依次执行：
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo 完成后再次双击 start.bat。已有视频无需重新下载。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause

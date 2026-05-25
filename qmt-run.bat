@echo off
title QMT-Executor
cd /d %~dp0

:: ====================================================
:: 配置说明：请将下方路径改为你真实的 QMT 安装目录和执行器目录
:: 注意：SINGLETON_PORT 必须与 Python 执行器主入口 main.py 中的 _SINGLETON_PORT (默认 59999) 保持一致！
:: ====================================================
set "QMT_ROOT=C:\path\to\your\qmt_install_dir"
set "EXECUTOR_DIR=%~dp0"
set "SINGLETON_PORT=59999"
:: ====================================================

set "USERDATA_MINI=%QMT_ROOT%\userdata_mini"
set "QMT_EXE=%QMT_ROOT%\bin.x64\XtItClient.exe"

:: 检查防重复启动（原子性一致性检测）
echo [检查] 正在检查系统运行状态...

set "EXECUTOR_RUNNING=0"
set "QMT_RUNNING=0"

:: A. 通过尝试绑定端口来检测 Python 执行器是否已在运行
python -c "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.bind(('127.0.0.1', %SINGLETON_PORT%))" >nul 2>&1 || set "EXECUTOR_RUNNING=1"

:: B. 检测该路径下的 QMT 客户端进程 (XtMiniQmt.exe) 是否已在运行
wmic process where "name='XtMiniQmt.exe'" get ExecutablePath 2>nul | find /i "%QMT_ROOT%" >nul && set "QMT_RUNNING=1"

:: 如果任意一个已启动，则进入警告逻辑
if "%EXECUTOR_RUNNING%"=="1" goto SHOW_WARNING
if "%QMT_RUNNING%"=="1" goto SHOW_WARNING
goto START_CLEANUP

:SHOW_WARNING
echo ====================================================
if "%EXECUTOR_RUNNING%"=="1" echo [警告] 检测到 Python 执行器已在运行中（端口 %SINGLETON_PORT% 已占用）！
if "%QMT_RUNNING%"=="1" echo [警告] 检测到该路径下的 QMT 客户端 (XtMiniQmt.exe) 已经在运行中！
echo [警告] 请先关闭已运行的程序。本窗口将在 5 秒后自动关闭...
echo ====================================================
ping -n 6 127.0.0.1 >nul
exit /b 0

:START_CLEANUP
:: 清理垃圾互斥锁、日志与崩溃文件
echo ====================================================
echo [清理] 开始清理以下目录中的垃圾文件：
echo ====================================================

if exist "%USERDATA_MINI%\log" (
    echo [清理] 正在清除日志目录：%USERDATA_MINI%\log
    rmdir /s /q "%USERDATA_MINI%\log" >nul 2>&1
    mkdir "%USERDATA_MINI%\log" >nul 2>&1
)
if exist "%USERDATA_MINI%\dumps" (
    echo [清理] 正在清除崩溃转储目录：%USERDATA_MINI%\dumps
    rmdir /s /q "%USERDATA_MINI%\dumps" >nul 2>&1
    mkdir "%USERDATA_MINI%\dumps" >nul 2>&1
)

echo [清理] 正在扫描并清除 *__mutex 锁文件...
if exist "%USERDATA_MINI%" (
    dir /b /s "%USERDATA_MINI%\*__mutex" 2>nul
    del /f /q /s "%USERDATA_MINI%\*__mutex" >nul 2>&1
)
:: 启动 miniQMT 客户端
echo [启动] 正在拉起 miniQMT 客户端...
if exist "%QMT_EXE%" (
    start "" "%QMT_EXE%"
) else (
    echo [错误] 未找到 QMT 核心程序，请检查 QMT_ROOT 路径配置: %QMT_EXE%
    pause
    exit /b 1
)

:: 就绪验证（自旋检测最多等待 60s）
echo [检测] 正在等待 miniQMT 登录就绪（自旋检测最多 60s）...
set "WAIT_COUNT=0"

:CHECK_LOOP
wmic process where "name='XtMiniQmt.exe'" get ExecutablePath 2>nul | find /i "%QMT_ROOT%" >nul
if not errorlevel 1 (
    echo [成功] 检测到 XtMiniQmt.exe 已运行！
    goto :START_PYTHON
)

set /a "WAIT_COUNT+=1"
if %WAIT_COUNT% geq 30 (
    echo ====================================================
    echo [错误] 启动超时（60s），任务失败，请手动检查！！！
    echo ====================================================
    pause
    exit /b 1
)

ping -n 3 127.0.0.1 >nul
goto :CHECK_LOOP

:START_PYTHON
:: 启动 Python 执行器
echo [启动] 正在启动当前目录下的 Python 执行器...
cd /d %EXECUTOR_DIR%
python main.py


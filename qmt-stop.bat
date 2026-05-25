@echo off
chcp 936 >nul
title 关闭QMT
cd /d %~dp0

:: ====================================================
:: QMT 客户端与 Python 执行器配置
:: ====================================================
:: 配置：QMT 窗口标题特征词（必填，用于极速识别及防多开误杀）
:: 获取：cmd(必须,powershell会乱码) 运行 tasklist /v | findstr "XtMini" 查看输出
:: 示例：输出结果最后一列即为窗口标题（如：88888888 - XX证券专业版）
:: 操作：从上述cmd的输出结果中，摘取能唯一代表该实例的词（如 88888888 或 XX证券）替换下方的 XXX
set "QMT_TITLE_KEYWORD=XXX"
set "HTTP_PORT=30015"
:: ====================================================

set "PORT_RUNNING=0"
set "QMT_RUNNING=0"

:: 检查 Python 执行器端口是否被占用
netstat -aon | findstr ":%HTTP_PORT% " | findstr "LISTENING" >nul && set "PORT_RUNNING=1"

:: 极速过滤查找指定关键字的 QMT 进程，结果输出到临时文件避开语法嵌套坑
tasklist /fi "imagename eq XtMiniQmt.exe" /v 2>nul | findstr "%QMT_TITLE_KEYWORD%" > "%TEMP%\qmt_grep.txt"
tasklist /fi "imagename eq XtItClient.exe" /v 2>nul | findstr "%QMT_TITLE_KEYWORD%" >> "%TEMP%\qmt_grep.txt"

:: 检查临时文件是否有匹配结果
for /f "usebackq tokens=2" %%a in ("%TEMP%\qmt_grep.txt") do (
    set "QMT_RUNNING=1"
)

:: 如果没有任何程序运行，提示并延时 5 秒后退出
if "%PORT_RUNNING%"=="0" (
    if "%QMT_RUNNING%"=="0" (
        echo ====================================================
        echo [提示] 未检测到运行的 QMT 客户端或 Python 执行器
        echo 窗口将在 5 秒后自动关闭...
        echo ====================================================
        del "%TEMP%\qmt_grep.txt" >nul 2>&1
        ping -n 6 127.0.0.1 >nul
        exit /b 0
    )
)

echo ====================================================
echo 正在停止 QMT 交易服务...
echo ====================================================

:: 执行 Python 执行器进程关闭
if "%PORT_RUNNING%"=="1" (
    echo [停止] 正在关闭 Python 执行器...
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%HTTP_PORT% " ^| findstr "LISTENING"') do (
        taskkill /f /pid %%a >nul 2>&1
        echo [成功] 已关闭 Python 进程，PID: %%a
    )
)

:: 执行指定 QMT 客户端进程关闭
if "%QMT_RUNNING%"=="1" (
    echo [停止] 正在关闭 QMT 客户端...
    for /f "usebackq tokens=2" %%a in ("%TEMP%\qmt_grep.txt") do (
        taskkill /f /pid %%a >nul 2>&1
        echo [成功] 已关闭 QMT 进程，PID: %%a
    )
)

del "%TEMP%\qmt_grep.txt" >nul 2>&1

echo ====================================================
echo QMT 交易服务已停止。
echo 窗口将在 5 秒后自动关闭...
echo ====================================================
ping -n 6 127.0.0.1 >nul
exit /b 0

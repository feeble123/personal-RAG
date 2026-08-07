@echo off
setlocal
title 水利知识库问答 - 后端服务
cd /d "%~dp0"

:: ===== 端口自检：自动清理占用 8000 的旧后端及子进程（防闪退/防双开）=====
set "PORT=8000"
echo [自检] 检查端口 %PORT% 是否被占用...
set "_FOUND=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /c:":%PORT% " ^| findstr /c:"LISTENING"') do (
    set "_FOUND=1"
    echo [自检] 停止占用端口的旧后端 PID=%%p 及子进程...
    taskkill /PID %%p /T /F >nul 2>&1
)
if "%_FOUND%"=="0" (
    echo [自检] 端口 %PORT% 空闲，直接启动。
) else (
    echo [自检] 旧后端已清理，稍候启动新实例...
    ping -n 2 127.0.0.1 >nul
)
set "_FOUND="

if not exist .venv\Scripts\python.exe (
    echo [错误] 未找到 .venv，正在创建虚拟环境，请稍候...
    pause
    exit /b 1
)

if not exist .env (
    echo [提示] 未找到 .env，正在从 .env.example 复制。请编辑填写 API Key 后重新启动。
    copy .env.example .env >nul
)

echo ============================================
echo   水利知识库问答 - 后端服务
echo   地址: http://localhost:8000
echo   文档: http://localhost:8000/api/docs
echo ============================================
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
pause

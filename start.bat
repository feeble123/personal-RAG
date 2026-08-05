@echo off
chcp 65001 >nul
title 水利知识库问答系统 - 一键启动
cd /d "%~dp0"

echo ============================================
echo   水利知识库问答系统 一键启动
echo   单机模式：前端构建产物由后端托管
echo   访问地址: http://localhost:8000
echo ============================================

REM 检查后端虚拟环境
if not exist backend\.venv\Scripts\python.exe (
    echo [错误] 后端虚拟环境不存在，请先运行:
    echo        cd backend ^&^& python -m venv .venv
    echo        .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

REM 检查 .env
if not exist backend\.env (
    echo [提示] 首次运行：正在复制 .env 模板。请编辑 backend\.env 填入 API Key。
    copy backend\.env.example backend\.env >nul
)

REM 构建前端（如 dist 不存在）
if not exist frontend\dist\index.html (
    echo [步骤] 首次构建前端（约 10 秒）...
    pushd frontend
    call npm install
    call npm run build
    popd
)

echo [步骤] 启动后端服务（前端已由后端托管）...
cd backend
start "" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
timeout /t 3 >nul
start http://localhost:8000
echo 服务已启动，浏览器将自动打开 http://localhost:8000
echo 关闭后端请关闭弹出的命令行窗口。
pause

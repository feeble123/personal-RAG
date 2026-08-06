@echo off
title 水利知识库问答 - 后端服务
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo [信息] 未找到 .venv，正在创建虚拟环境，请稍候...
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

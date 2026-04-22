@echo off
chcp 65001 >nul 2>&1
echo ============================================================
echo   产品立项审核系统 — 一键部署脚本
echo ============================================================
echo.

:: ---- 1. 检查 Python ----
echo [1/4] 检查 Python 环境...
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   ❌ 未检测到 Python！请先安装 Python 3.10+
    echo.
    echo   下载地址: https://www.python.org/downloads/
    echo   ⚠️ 安装时务必勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version 2^>^nul') do set PY_VER=%%i
echo   ✅ 检测到 %PY_VER%

:: 检查版本号 >= 3.10
python -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   ❌ Python 版本过低，需要 3.10 或以上
    pause
    exit /b 1
)

:: ---- 2. 创建虚拟环境 ----
echo.
echo [2/4] 创建虚拟环境...
if exist ".venv\Scripts\python.exe" (
    echo   ✅ 虚拟环境已存在，跳过创建
) else (
    python -m venv .venv
    if %ERRORLEVEL% neq 0 (
        echo   ❌ 创建虚拟环境失败
        pause
        exit /b 1
    )
    echo   ✅ 虚拟环境创建成功
)

:: ---- 3. 安装依赖 ----
echo.
echo [3/4] 安装 Python 依赖（使用阿里云镜像）...
.venv\Scripts\python.exe -m pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ >nul 2>&1
.venv\Scripts\pip.exe install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
if %ERRORLEVEL% neq 0 (
    echo   ❌ 依赖安装失败，请检查网络连接
    pause
    exit /b 1
)
echo   ✅ 依赖安装完成

:: ---- 4. 初始化 ----
echo.
echo [4/4] 初始化数据库和目录...
if not exist "data" mkdir data
if not exist "uploads" mkdir uploads
if not exist "output" mkdir output
if not exist "static" mkdir static
echo   ✅ 目录就绪

:: ---- 完成 ----
echo.
echo ============================================================
echo   ✅ 部署完成！
echo ============================================================
echo.
echo   启动 Web 服务:
echo     .venv\Scripts\python.exe web_app.py
echo.
echo   启动飞书 Bot:
echo     .venv\Scripts\python.exe run_feishu_bot.py
echo.
echo   首次使用前，请复制 .env.example 为 .env 并填写配置:
echo     copy .env.example .env
echo.
pause

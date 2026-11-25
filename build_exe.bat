@echo off
chcp 936 >nul
cd /d %~dp0

if not exist .venv\Scripts\python.exe (
    echo [错误] 未找到 .venv，请先创建虚拟环境并安装 PySide6。
    pause
    exit /b 1
)

echo [1/3] 安装/更新 PyInstaller...
.venv\Scripts\python.exe -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo [错误] PyInstaller 安装失败。
    pause
    exit /b 1
)

echo [2/3] 清理旧构建...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [3/3] 打包 one-folder 版本...
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --noupx --name ChargerSim --paths . app\main.py
if errorlevel 1 (
    echo [错误] 打包失败。
    pause
    exit /b 1
)

copy /Y README.md dist\ChargerSim\README.md >nul

echo.
echo 打包完成：
echo   目录：dist\ChargerSim
echo   程序：dist\ChargerSim\ChargerSim.exe
echo.
pause

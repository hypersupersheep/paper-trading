@echo off
REM 在 Windows 上运行,产出 dist\PaperTrading\PaperTrading.exe 和可分享的 zip。
REM 前置:已装 Python 3 + 依赖(python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt pyinstaller)
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pyinstaller.exe" (
  echo [x] 未找到 .venv\Scripts\pyinstaller.exe
  echo     请先: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt pyinstaller
  exit /b 1
)

echo ==^> 清理旧产物
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo ==^> PyInstaller 打包
.venv\Scripts\pyinstaller --noconfirm --clean paper_trading.spec
if errorlevel 1 ( echo [x] 打包失败 & exit /b 1 )

for /f "delims=" %%v in ('.venv\Scripts\python -c "from backend.version import __version__; print(__version__)"') do set VERSION=%%v
set ZIP=dist\PaperTrading-v%VERSION%-windows.zip

echo ==^> 打包 zip: %ZIP%
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\PaperTrading' -DestinationPath '%ZIP%'"

echo.
echo 完成 ✅
echo   产物:       dist\PaperTrading\PaperTrading.exe
echo   分享给同事: %ZIP%
echo   同事用法:   解压 -^> 双击 PaperTrading.exe(首次启动弹出原生窗口)
endlocal

@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
  echo 依赖安装失败。
  pause
  exit /b 1
)

%PY% -m PyInstaller --noconfirm --clean --onedir --console ^
  --name "回单裁切v2" ^
  --distpath dist --workpath build ^
  --exclude-module numpy --exclude-module pandas ^
  --exclude-module lxml --exclude-module openpyxl ^
  receipt_crop.py
if errorlevel 1 (
  echo 打包失败。
  pause
  exit /b 1
)

echo.
echo 完成：dist\回单裁切v2\回单裁切v2.exe
pause

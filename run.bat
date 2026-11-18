@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if "%~1"=="" (
  echo 把银行回单 PDF 拖到 run.bat 上运行。
  echo.
  echo 也可以命令行运行：run.bat 回单1.pdf 回单2.pdf
  echo 输出会放在第一个 PDF 所在的文件夹。
  pause
  exit /b 1
)

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo 没有找到 Python。请先安装 Python 3.9 或更高版本：
  echo https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

%PY% -c "import pdfplumber, pypdf, pypdfium2, PIL" >nul 2>nul
if errorlevel 1 (
  echo 首次运行：正在安装 pdfplumber、pypdf、pypdfium2、Pillow...
  %PY% -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo.
    echo 安装失败。请检查网络，或手动执行：
    echo %PY% -m pip install -r "%~dp0requirements.txt"
    pause
    exit /b 1
  )
)

%PY% "%~dp0receipt_crop.py" %*
set "RC=%errorlevel%"
echo.
if not "%RC%"=="0" echo 处理失败，请把上面的错误信息发给同事。
if not defined RECEIPT_CROP_NO_PAUSE pause
exit /b %RC%

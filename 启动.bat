@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Video Thumb Lister - Windows GUI launcher (double-click)
rem   - 启动后会弹出「选择目录」对话框，选定即开始扫描
rem   - 也可用命令行：run.bat "D:\path\to\videos" [--force]
rem   - 若想用命令行（无界面）版本，请直接运行：
rem       python video_thumb_lister.py "D:\path\to\videos"
rem ============================================================

set "SCRIPT=%~dp0video_thumb_lister_gui.py"

rem --- locate a Python interpreter (prefer pythonw for no console) --------
set "PY="
if exist "C:\Users\excel\AppData\Local\Programs\Python\Python313\pythonw.exe" (
    set "PY=C:\Users\excel\AppData\Local\Programs\Python\Python313\pythonw.exe"
    goto :got_py
)
if exist "C:\Users\excel\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" (
    set "PY=C:\Users\excel\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
    goto :got_py
)
where pyw >nul 2>nul && ( set "PY=pyw" & goto :got_py )
where pythonw >nul 2>nul && ( set "PY=pythonw" & goto :got_py )
rem fallback to python (shows a brief console window)
if exist "C:\Users\excel\AppData\Local\Programs\Python\Python313\python.exe" (
    set "PY=C:\Users\excel\AppData\Local\Programs\Python\Python313\python.exe"
    goto :got_py
)
where py >nul 2>nul && ( set "PY=py" & goto :got_py )
where python >nul 2>nul && ( set "PY=python" & goto :got_py )

echo [ERROR] Python not found. Please install Python 3 and retry.
pause
exit /b 1
:got_py

rem --- run the GUI (start "" detaches it so this window closes) ----------
start "" "%PY%" "%SCRIPT%" %*
endlocal

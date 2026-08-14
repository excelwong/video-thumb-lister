@echo off
setlocal

REM ============================================================
REM  Run this script ONCE as Administrator to register the HTML
REM  Help Workshop COM components so hhc.exe can build CHM files
REM  without HHC6003 or Failed to create compiler object.
REM
REM  IMPORTANT: hhc.exe is a 32bit program, so its DLLs must be
REM  registered with the 32bit regsvr32 in SysWOW64.
REM  Using the 64bit regsvr32 in System32 will FAIL for these DLLs.
REM
REM  Only DLLs that EXPORT DllRegisterServer need registering:
REM    itcc.dll  (TOC/index compiler)  - registers OK
REM    itircl.dll / itss.dll (system)  - usually already registered
REM  hha.dll does NOT export DllRegisterServer; hhc.exe loads it
REM  directly from its own folder, so do NOT run regsvr32 on it.
REM ============================================================

set "RS=%SystemRoot%\SysWOW64\regsvr32.exe"
if not exist "%RS%" set "RS=%SystemRoot%\System32\regsvr32.exe"

echo Using: %RS%
echo(

call :reg "%~dp0itcc.dll" "itcc.dll TOC/index compiler"
call :reg "%SystemRoot%\SysWOW64\itircl.dll" "itircl.dll system index"
call :reg "%SystemRoot%\SysWOW64\itss.dll"   "itss.dll system storage"

echo(
echo Note: hha.dll is loaded directly by hhc.exe, no registration needed.
echo(
echo ============================================================
echo  All done. If any step reported FAILED, rerun this script as
echo  Administrator and confirm itcc.dll is in this folder.
echo ============================================================
pause
goto :eof

:reg
set "TARGET=%~1"
if not exist "%TARGET%" (
    echo [SKIP] %~2 file not found: %TARGET%
    exit /b 0
)
"%RS%" /s "%TARGET%"
if errorlevel 1 (
    echo [FAIL] %~2 regsvr32 returned %errorlevel%
) else (
    echo [ OK ] %~2
)
exit /b 0

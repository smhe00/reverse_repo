@echo off
setlocal EnableDelayedExpansion

set "MANAGER=%~dp0scripts\manage_reverse_repo_tasks.ps1"
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "PSARGS=-NoProfile -ExecutionPolicy Bypass -File"

if "%~1"=="" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Help
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="init" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Initialize
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="add" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Install
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="del" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Remove
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="stat" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Status
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="on" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Enable
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="off" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Disable
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="mail" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action ConfigureMail
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="mt" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action TestMail
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="reset" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action ResetCertificate
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="cert" (
    if /I "%~2"=="off" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action CertDisable
    ) else if /I "%~2"=="del" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action CertRemove
    ) else if /I "%~2"=="stat" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action CertStatus
    ) else if "%~2"=="" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Cert
    ) else (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Cert -CertDate "%~2"
    )
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="stress" (
    if /I "%~2"=="off" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action StressDisable
    ) else if /I "%~2"=="del" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action StressRemove
    ) else if /I "%~2"=="stat" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action StressStatus
    ) else if "%~2"=="" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Stress
    ) else (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Stress -StressDate "%~2"
    )
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="help" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Help
    exit /b !ERRORLEVEL!
)

"%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Help
echo.
echo Unknown argument: %~1
exit /b 2

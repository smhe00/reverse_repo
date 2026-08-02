@echo off
setlocal EnableDelayedExpansion

set "MANAGER=%~dp0scripts\manage_reverse_repo_tasks.ps1"

if "%~1"=="" (
    pwsh -NoProfile -File "%MANAGER%" -Action Help
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="add" (
    pwsh -NoProfile -File "%MANAGER%" -Action Install
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="del" (
    pwsh -NoProfile -File "%MANAGER%" -Action Remove
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="stat" (
    pwsh -NoProfile -File "%MANAGER%" -Action Status
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="on" (
    pwsh -NoProfile -File "%MANAGER%" -Action Enable
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="off" (
    pwsh -NoProfile -File "%MANAGER%" -Action Disable
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="mail" (
    pwsh -NoProfile -File "%MANAGER%" -Action ConfigureMail
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="mt" (
    pwsh -NoProfile -File "%MANAGER%" -Action TestMail
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="reset" (
    pwsh -NoProfile -File "%MANAGER%" -Action ResetCertificate
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="stress" (
    if /I "%~2"=="off" (
        pwsh -NoProfile -File "%MANAGER%" -Action StressDisable
    ) else if /I "%~2"=="del" (
        pwsh -NoProfile -File "%MANAGER%" -Action StressRemove
    ) else if /I "%~2"=="stat" (
        pwsh -NoProfile -File "%MANAGER%" -Action StressStatus
    ) else if "%~2"=="" (
        pwsh -NoProfile -File "%MANAGER%" -Action Stress
    ) else (
        pwsh -NoProfile -File "%MANAGER%" -Action Stress -StressDate "%~2"
    )
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="help" (
    pwsh -NoProfile -File "%MANAGER%" -Action Help
    exit /b !ERRORLEVEL!
)

pwsh -NoProfile -File "%MANAGER%" -Action Help
echo.
echo Unknown argument: %~1
exit /b 2

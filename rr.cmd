@echo off
setlocal EnableDelayedExpansion

set "MANAGER=%~dp0scripts\manage_reverse_repo_tasks.ps1"
set "UPDATER=%~dp0scripts\update_reverse_repo.ps1"
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "PSARGS=-NoProfile -ExecutionPolicy Bypass -File"

if "%~1"=="" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Help
    set "RESULT=!ERRORLEVEL!"
    echo.
    echo   .\rr cfg
    echo       Edit strategy parameters safely; requires rr off and runs verify.
    echo   .\rr ui
    echo       Open the local-only web console for guided status and operations.
    exit /b !RESULT!
)
if /I "%~1"=="init" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Initialize
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="up" (
    "%POWERSHELL%" %PSARGS% "%UPDATER%" -Destination "%~dp0."
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
if /I "%~1"=="clear" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Clear
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
if /I "%~1"=="cfg" (
    "%POWERSHELL%" %PSARGS% "%~dp0scripts\configure_reverse_repo_strategy.ps1"
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="ui" (
    "%POWERSHELL%" %PSARGS% "%~dp0scripts\run_reverse_repo_web_ui.ps1"
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
if /I "%~1"=="wx" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action ConfigureWxPusher
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="wt" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action TestWxPusher
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="cert" (
    if /I "%~2"=="stat" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action LiveCertStatus
    ) else if /I "%~2"=="reset" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action LiveCertReset
    ) else if "%~2"=="" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action LiveCert
    ) else (
        echo Unknown certification argument: %~2
        echo Supported: stat, reset
        exit /b 2
    )
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="dev" (
    if /I "%~2"=="bind" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevBind
    ) else if /I "%~2"=="status" (
        "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevStatus
    ) else if /I "%~2"=="cert" (
        if /I "%~3"=="stat" (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevCertStatus
        ) else if /I "%~3"=="off" (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevCertDisable
        ) else if /I "%~3"=="del" (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevCertRemove
        ) else if /I "%~3"=="reset" (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevCertReset
        ) else if "%~3"=="" (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevCert
        ) else (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevCert -CertDate "%~3"
        )
    ) else if /I "%~2"=="stress" (
        if /I "%~3"=="stat" (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevStressStatus
        ) else if /I "%~3"=="off" (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevStressDisable
        ) else if /I "%~3"=="del" (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevStressRemove
        ) else if "%~3"=="" (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevStress
        ) else (
            "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action DevStress -StressDate "%~3"
        )
    ) else (
        echo Unknown developer argument: %~2
        echo Supported: bind, status, cert [date|stat|off|del|reset], stress [date|stat|off|del]
        exit /b 2
    )
    exit /b !ERRORLEVEL!
)
if /I "%~1"=="help" (
    "%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Help
    set "RESULT=!ERRORLEVEL!"
    echo.
    echo   .\rr cfg
    echo       Edit strategy parameters safely; requires rr off and runs verify.
    echo   .\rr ui
    echo       Open the local-only web console for guided status and operations.
    exit /b !RESULT!
)

"%POWERSHELL%" %PSARGS% "%MANAGER%" -Action Help
echo.
echo Unknown argument: %~1
exit /b 2

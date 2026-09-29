@echo off
setlocal enabledelayedexpansion

set "folder=%~1"
if "%folder%"=="" set "folder=%cd%"

pushd "%folder%" 2>nul || (
    echo [ERROR] Cannot enter folder: %folder%
    pause
    exit /b 1
)

set /a count=0
set /a skip=0

for %%F in (".trashed-*") do (
    set "oldname=%%~nxF"

    set "name=!oldname:.trashed-=%!"

    set "newname="
    for /f "tokens=1,* delims=-" %%a in ("!name!") do set "newname=%%b"
    if "!newname!"=="" set "newname=!name!"

    if "!newname!"=="!oldname!" (
        echo [SKIP] !oldname!  ^(no change^)
    ) else (
        if exist "!newname!" (
            echo [CONFLICT] !newname! exists, skip !oldname!
            set /a skip+=1
        ) else (
            ren "!oldname!" "!newname!"
            if errorlevel 1 (
                echo [FAIL] !oldname!
                set /a skip+=1
            ) else (
                echo [OK] !oldname!  -^>  !newname!
                set /a count+=1
            )
        )
    )
)

echo.
echo ================================
echo  Done: success=!count!  skip/fail=!skip!
echo ================================
popd
pause
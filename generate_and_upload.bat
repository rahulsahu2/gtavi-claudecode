@echo off
setlocal

pushd "%~dp0"
if errorlevel 1 (
    echo ERROR: Could not open the project folder.
    exit /b 1
)

set "CLIENT_SECRETS="
for %%F in ("%~dp0client_secret_*.json") do (
    if not defined CLIENT_SECRETS if exist "%%~fF" set "CLIENT_SECRETS=%%~fF"
)

if not defined CLIENT_SECRETS (
    echo ERROR: No client_secret_*.json file found in the project folder.
    popd
    exit /b 1
)

python "%~dp0scripts\run.py" --clips 3 --youtube-upload --youtube-client-secrets "%CLIENT_SECRETS%"
set "EXIT_CODE=%ERRORLEVEL%"

popd
exit /b %EXIT_CODE%

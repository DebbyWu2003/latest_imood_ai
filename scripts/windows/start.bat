@echo off
rem Start the demo for one mode:   start.bat [vendor|full]      (default: imood\pipeline.yaml "mode", or env MODE)
rem   vendor = LiveTalking only
rem   full   = our pipeline: LiveTalking + BERT emotion + voice service; also starts llama-server (Windows) if needed
call "%~dp0..\..\config\paths.bat"
if not defined PROJECT_WSL (echo ERROR: could not reach WSL distro %WSL_DISTRO% & exit /b 1)
if not "%~1"=="" set MODE=%~1
if /i "%MODE%"=="full" call :llama
wsl -d %WSL_DISTRO% -- bash "%PROJECT_WSL%/scripts/wsl/start.sh" %MODE%
start "" %FRONTEND_URL%
exit /b 0

:llama
curl -s --max-time 3 http://localhost:%LLAMA_PORT%/health | find "ok" >nul && goto :eof
echo starting llama-server in its own window...
start "llama-server" cmd /k "%~dp0start_llama.bat"
for /l %%i in (1,1,60) do (
  curl -s --max-time 2 http://localhost:%LLAMA_PORT%/health | find "ok" >nul && goto :eof
  ping -n 3 127.0.0.1 >nul
)
echo WARNING: llama-server not ready after ~2 min - check the llama-server window
goto :eof

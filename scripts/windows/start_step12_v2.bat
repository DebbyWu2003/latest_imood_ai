@echo off
rem Step 12 v2: llama-server (Windows) + LiveTalking/adapter + voice service (WSL %WSL_DISTRO%), then open the imood page.
call "%~dp0..\..\config\paths.bat"
if not defined PROJECT_WSL (echo ERROR: could not reach WSL distro %WSL_DISTRO% & exit /b 1)
curl -s --max-time 3 http://localhost:%LLAMA_PORT%/health | find "ok" >nul && goto llama_ready
echo starting llama-server in its own window...
start "llama-server" cmd /k "%~dp0start_llama.bat"
for /l %%i in (1,1,60) do (
  curl -s --max-time 2 http://localhost:%LLAMA_PORT%/health | find "ok" >nul && goto llama_ready
  ping -n 3 127.0.0.1 >nul
)
echo WARNING: llama-server not ready after ~2 min - check the llama-server window
:llama_ready
echo llama-server: OK
wsl -d %WSL_DISTRO% -- bash "%PROJECT_WSL%/scripts/wsl/start_step12_v2.sh"
start "" %FRONTEND_URL%

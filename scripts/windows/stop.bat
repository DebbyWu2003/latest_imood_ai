@echo off
rem Stop everything of either mode: WSL services + llama-server.
call "%~dp0..\..\config\paths.bat"
wsl -d %WSL_DISTRO% -- bash "%PROJECT_WSL%/scripts/wsl/stop.sh"
taskkill /IM llama-server.exe /F >nul 2>&1 && echo llama-server stopped || echo llama-server not running
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader

@echo off
rem Stop the whole cyber_gf demo: WSL services + llama-server. Leaves the GPU at desktop-only usage.
call "%~dp0..\..\config\paths.bat"
wsl -d %WSL_DISTRO% -- bash "%PROJECT_WSL%/scripts/wsl/stop_avatar_services.sh"
taskkill /IM llama-server.exe /F >nul 2>&1 && echo llama-server stopped || echo llama-server not running
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader

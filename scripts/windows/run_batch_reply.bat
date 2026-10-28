@echo off
rem Step 12 MVP (fallback, non-streaming) - runs in WSL %WSL_DISTRO%, never the default distro.
call "%~dp0..\..\config\paths.bat"
wsl -d %WSL_DISTRO% -- bash "%PROJECT_WSL%/scripts/wsl/run_batch_reply.sh" %*

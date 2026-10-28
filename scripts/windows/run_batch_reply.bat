@echo off
rem Batch reply (non-streaming) - runs in WSL %WSL_DISTRO%, never the default distro.
call "%~dp0..\..\config\paths.bat"
wsl -d %WSL_DISTRO% -- bash "%PROJECT_WSL%/scripts/wsl/run_batch_reply.sh" %*

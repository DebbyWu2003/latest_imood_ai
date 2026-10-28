# Stage 1-2 VRAM measurement (Windows side) + launches the WSL TTS perf run.
# Stage 1: no cyber_gf model loaded (llama-server stopped). Stage 2: llama-server loaded.
# Benchmark tool (not needed to run the demo). Defaults are the standard locations; override with parameters.
param([int]$Runs = 3,
      [string]$LlamaDir = "E:\llama.cpp",
      [string]$LlamaModel = "E:\llama.cpp\models\Qwen3-14B-Q4_K_M.gguf",
      [string]$WslDistro = "Ubuntu-24.04")
$ErrorActionPreference = "Stop"
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$rootWsl = (wsl -d $WslDistro --exec wslpath -a ($root -replace '\\', '/')).Trim()
$out  = Join-Path $root "outputs\perf"; New-Item -ItemType Directory -Force $out | Out-Null

function Smi { [int](nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits) }
function GpuProcs {
  (Get-Counter "\GPU Process Memory(*)\Dedicated Usage").CounterSamples | Where-Object { $_.CookedValue -gt 20MB } |
    Sort-Object CookedValue -Descending | ForEach-Object {
      $procId = [int]($_.InstanceName -replace '^pid_(\d+)_.*', '$1')
      [pscustomobject]@{ pid = $procId; name = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName; dedicated_mib = [math]::Round($_.CookedValue / 1MB) }
    }
}

# stage 1
Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 5
$s1 = Smi; $p1 = @(GpuProcs)

# stage 2
$llama = Start-Process -FilePath (Join-Path $LlamaDir "llama-server.exe") -ArgumentList "-m",$LlamaModel,"-np","1","-c","8192","-fa","on","--temp","1.0","--top-p","0.95","--host","0.0.0.0","--port","8090" -WorkingDirectory $LlamaDir -RedirectStandardOutput (Join-Path $LlamaDir "server_out.log") -RedirectStandardError (Join-Path $LlamaDir "server_err.log") -WindowStyle Hidden -PassThru
for ($i = 0; $i -lt 60; $i++) { Start-Sleep 2; try { if ((Invoke-RestMethod http://127.0.0.1:8090/health -TimeoutSec 2).status -eq 'ok') { break } } catch {} }
Start-Sleep 3
$s2 = Smi; $p2 = @(GpuProcs)

$ext = [ordered]@{ "1_baseline_no_cyber_gf_models_mib" = $s1; "2_after_llama_load_mib" = $s2; "llama_delta_mib" = ($s2 - $s1);
                   "gpu_processes_stage1" = $p1; "gpu_processes_stage2" = $p2; "llama_pid" = $llama.Id }
$ext | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $out "stages_windows.json")
$json = ($ext | ConvertTo-Json -Depth 4 -Compress)
$json | Set-Content -Encoding ascii (Join-Path $out "_ext.json")

# stages 3-7 in WSL; per-process snapshot taken while it runs
$job = Start-Job { param($r, $d, $p) wsl -d $d -- bash "$p/scripts/wsl/perf/run_tts_perf.sh" --runs $r 2>&1 } -ArgumentList $Runs, $WslDistro, $rootWsl
$snap = $null
while ($job.State -eq 'Running') {
  Start-Sleep 5
  $o = Receive-Job $job -Keep | Out-String
  if (-not $snap -and $o -match 'RUN .*"tag": "run1"') { $snap = @(GpuProcs) }
}
Receive-Job $job | Set-Content -Encoding utf8 (Join-Path $out "tts_perf.log")
if ($snap) { $snap | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $out "gpu_processes_during_tts.json") }
"done"

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  Write-Host "未找到 .venv。请先执行：" -ForegroundColor Red
  Write-Host "  python -m venv .venv" -ForegroundColor Yellow
  Write-Host "  .venv\Scripts\python.exe -m pip install -e .[dev]" -ForegroundColor Yellow
  exit 1
}

New-Item -ItemType Directory -Force -Path (Join-Path $root "var") | Out-Null

# 后端必须在沙箱之外启动：12306 的 MCP 是一个子进程，
# 受限环境会拦掉它，表现为 "server closed stdout"。
Write-Host "启动后端 (uvicorn) ..." -ForegroundColor Cyan
Start-Process -FilePath $python `
  -ArgumentList "-m","uvicorn","app.api.app:create_app","--factory","--host","127.0.0.1","--port","8000" `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $root "var\api.out.log") `
  -RedirectStandardError  (Join-Path $root "var\api.err.log")

Write-Host "启动前端 (vite) ..." -ForegroundColor Cyan
Start-Process -FilePath "cmd.exe" `
  -ArgumentList "/c","npm run dev > ..\var\web.out.log 2>&1" `
  -WorkingDirectory (Join-Path $root "web") -WindowStyle Hidden

$ready = $false
foreach ($i in 1..25) {
  Start-Sleep -Milliseconds 800
  try {
    $null = Invoke-WebRequest -Uri "http://127.0.0.1:8000/healthz" -UseBasicParsing -TimeoutSec 2
    $null = Invoke-WebRequest -Uri "http://127.0.0.1:5173/" -UseBasicParsing -TimeoutSec 2
    $ready = $true
    break
  } catch { }
}

$health = $null
try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/healthz" -TimeoutSec 4 } catch { }

Write-Host ""
if ($ready) {
  Write-Host "  前端    http://127.0.0.1:5173" -ForegroundColor Green
} else {
  Write-Host "  前端未就绪，检查 var\web.out.log" -ForegroundColor Yellow
}
if ($health) {
  $llm = $health.llm_runtime.active_client
  $ch  = if ($health.channels) { "已接入" } else { "未接入" }
  Write-Host "  后端    能力 $($health.capabilities) 项 · 通道 $ch · 模型 $llm" -ForegroundColor Green
}
Write-Host ""
Write-Host "  日志    var\api.err.log  ·  var\web.out.log" -ForegroundColor DarkGray
Write-Host "  停止    .\scripts\stop-dev.ps1" -ForegroundColor DarkGray
Write-Host ""

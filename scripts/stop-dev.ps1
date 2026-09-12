$root = Split-Path -Parent $PSScriptRoot
$venv = (Resolve-Path (Join-Path $root ".venv\Scripts\python.exe") -ErrorAction SilentlyContinue).Path

$stopped = 0
if ($venv) {
  Get-Process -Name python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $venv } |
    ForEach-Object { Stop-Process -Id $_.Id -Force; $stopped++ }
}

# 只结束本项目的 vite，不碰机器上其它 node 进程。
Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*vite*" -and $_.CommandLine -like "*travel-plan agent*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $stopped++ }

# MCP 子进程（本地安装的 node 服务）
Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*var\mcp*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $stopped++ }

Write-Host "已停止 $stopped 个进程。" -ForegroundColor Yellow

# 把 MCP 服务端装到项目本地（var/mcp），让运行时不必依赖 npx。
#
# 为什么必须这样：从长期运行的服务里拉 npx 在 Windows 上不可靠 ——
# npx 是批处理垫片、路径含空格、父进程可能没有控制台，三者叠加会让
# 子进程刚启动就退出（表现为 "server closed stdout"）。本地安装后用
# `node <entry>` 直接启动，三个变量一起消失，顺带把版本钉死。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$packages = @("12306-mcp")
foreach ($package in $packages) {
  Write-Host "安装 $package -> var\mcp" -ForegroundColor Cyan
  npm install --no-audit --no-fund --prefix "var\mcp" $package
}

Write-Host ""
Write-Host "完成。可运行的 MCP 服务端：" -ForegroundColor Green
Get-ChildItem "var\mcp\node_modules" -Directory -ErrorAction SilentlyContinue |
  Where-Object { Test-Path (Join-Path $_.FullName "package.json") } |
  ForEach-Object { Write-Host "  - $($_.Name)" }

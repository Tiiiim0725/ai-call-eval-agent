# ============================================================
# AI 电话评价 Agent - 一键启动脚本
# 后端端口 8898 | 前端端口 8897
# ============================================================
$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendRoot = Join-Path $scriptRoot 'backend'
$frontendRoot = Join-Path $scriptRoot 'frontend'

# 端口被占用时停止启动，避免误杀其他项目或启动出前后端版本错位。
foreach ($port in @(8898, 8897)) {
    $line = netstat -ano | Select-String ":${port}.*LISTENING"
    if ($line) {
        $oldPid = ($line -split '\s+')[-1] | Select-Object -First 1
        throw "端口 $port 已被进程 $oldPid 占用。请确认并关闭对应项目后重试。"
    }
}

# 启动后端
Start-Process -FilePath 'python' -ArgumentList 'app.py' -WorkingDirectory $backendRoot -WindowStyle Hidden
Write-Host '后端启动中: http://127.0.0.1:8898/' -ForegroundColor Cyan

# 启动前端
Start-Process -FilePath 'python' -ArgumentList @('-m', 'http.server', '8897', '--bind', '127.0.0.1', '--directory', $frontendRoot) -WindowStyle Hidden
Write-Host '前端启动中: http://127.0.0.1:8897/' -ForegroundColor Cyan

Start-Sleep -Seconds 2
Start-Process 'http://127.0.0.1:8897/'
Write-Host '浏览器已打开' -ForegroundColor Green
Write-Host '后端/前端在隐藏进程中运行；再次启动前请先确认并关闭对应端口进程。' -ForegroundColor Yellow

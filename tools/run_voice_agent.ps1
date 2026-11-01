#Requires -Version 7.0
<#
    一键启动"小狗语音代理"（PowerShell 7）。

    桌面图标双击时等价于：
        cd E:\zongy\WORK\Claude code\FPGAandDOG
        python tools\voice_agent.py --allow-motion

    手动试用：
        pwsh -NoProfile -File tools\run_voice_agent.ps1                 # 会走近卡片
        pwsh -NoProfile -File tools\run_voice_agent.ps1 -Mode readonly  # 只读不动
        pwsh -NoProfile -File tools\run_voice_agent.ps1 -Duration 60    # 跑 60 秒自动结束
#>
[CmdletBinding()]
param(
    # walk = 允许走近卡片（真机演示）；readonly = 只听 + 读数字，绝不移动
    [ValidateSet('walk', 'readonly')]
    [string]$Mode = 'walk',
    # 不开语音播报，只看日志
    [switch]$NoTts,
    # >0 时到点自动退出（测试用）；0 = 一直听直到说"小狗下班"或 Ctrl+C
    [int]$Duration = 0
)

$ErrorActionPreference = 'Stop'

# 只允许一个语音代理在跑。2026-11-01 15:29 同时开了两个窗口：两个代理抢同一只
# 麦克风、同一台相机、同一只狗，日志里出现了 "camera grab failed"、"seek turn
# failed: dog state is 18.70 s old"、"seek look failed: timed out" 这类互相打架的
# 报错，其中一个窗口还会先说出"好的，我去找数字八"再做一半就没了。
$mutex = [System.Threading.Mutex]::new($false, 'Global\FPGAandDOG_VoiceAgent')
$ownsMutex = $false
try {
    $ownsMutex = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $ownsMutex = $true      # 上一个窗口是崩掉的，接管
}
if (-not $ownsMutex) {
    Write-Host ''
    Write-Host '已经有一个「小狗-听我说话」窗口在运行了。' -ForegroundColor Red
    Write-Host '请先回到那个窗口按 Ctrl+C（或说"小狗下班"）关掉它，再启动新的。' -ForegroundColor Yellow
    Write-Host '两个窗口同时跑会抢麦克风、相机和狗，动作会互相打断。' -ForegroundColor Gray
    Read-Host '按回车关闭'
    exit 3
}

$root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $root

# 中文日志/播报在本窗口里必须能正常显示
$env:PYTHONIOENCODING = 'utf-8'
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

# 用创建图标时就验证过的解释器，退回 PATH 里的 python
$python = 'D:\zongy\anacode\python.exe'
if (-not (Test-Path $python)) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) {
        Write-Host '找不到 python.exe，请先确认 Python 已安装。' -ForegroundColor Red
        Read-Host '按回车关闭'
        exit 1
    }
    $python = $found.Source
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "voice_agent_$stamp.log"

$arguments = @('tools\voice_agent.py', '--log', $log)
if ($Mode -eq 'walk') { $arguments += '--allow-motion' }
if ($NoTts) { $arguments += '--no-tts' }
if ($Duration -gt 0) { $arguments += @('--duration', $Duration) }

Write-Host ''
Write-Host '=====================================================' -ForegroundColor Cyan
Write-Host '  小狗语音代理' -ForegroundColor Cyan
Write-Host ('  模式：' + $(if ($Mode -eq 'walk') { '会走近卡片（狗会动）' } else { '只读不动（安全）' })) -ForegroundColor Cyan
Write-Host '  唤醒：小狗，看看是什么数字' -ForegroundColor Gray
Write-Host '  退出：说"小狗，下班" 或按 Ctrl+C' -ForegroundColor Gray
Write-Host ('  日志：' + $log) -ForegroundColor Gray
Write-Host '=====================================================' -ForegroundColor Cyan

if ($Mode -eq 'walk') {
    Write-Host '提示：确认狗已站立、周围 1 米内无障碍，2 秒后开始……' -ForegroundColor Yellow
    Start-Sleep -Seconds 2
}

& $python @arguments
$code = $LASTEXITCODE

Write-Host ''
if ($code -eq 0) {
    Write-Host "已退出。日志：$log" -ForegroundColor Green
} else {
    Write-Host "已退出（退出码 $code）。日志：$log" -ForegroundColor Red
}

# 双击启动时窗口会立刻关闭，留一步让人看清日志路径
if ($Duration -eq 0) { Read-Host '按回车关闭窗口' | Out-Null }
if ($mutex) { $mutex.ReleaseMutex(); $mutex.Dispose() }
exit $code

# 停止本项目的 TeleOps 进程（不会误伤其他 Python 程序）
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot

# 从 config.yaml 里读端口，读不到就用默认值
$port = 8800
$cfg = Join-Path $root 'config.yaml'
if (Test-Path $cfg) {
    $m = Select-String -Path $cfg -Pattern '^\s*port:\s*(\d+)' | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}

$targets = @{}

# 途径一：命令行里带 run.py 的 Python 进程。
# 注意不能只按"命令行包含项目路径"来判断——从项目目录直接 `python run.py`
# 启动时，命令行里只有 run.py，不含任何路径。所以这里再看一眼可执行文件
# 是不是本项目的 venv，两者满足其一即认。
$procs = Get-CimInstance Win32_Process -Filter "name='python.exe' or name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*run.py*' }
foreach ($p in $procs) {
    if ($p.CommandLine -like "*$root*" -or $p.ExecutablePath -like "$root*") {
        $targets[[int]$p.ProcessId] = $p.ExecutablePath
    }
}

# 途径二：谁占着后台端口，谁就是当前这份服务——覆盖掉从别处启动、
# 命令行看不出归属的情况。
$owner = Get-NetTCPConnection -State Listen -LocalPort $port | Select-Object -First 1
if ($owner) {
    $op = Get-Process -Id $owner.OwningProcess
    if ($op -and $op.ProcessName -in @('python', 'pythonw')) {
        $targets[[int]$op.Id] = $op.Path
    }
}

if ($targets.Count -eq 0) {
    Write-Host "没有找到正在运行的 TeleOps 进程。"
    exit 0
}

foreach ($id in $targets.Keys) {
    Write-Host ("正在结束进程 {0}" -f $id)
    Stop-Process -Id $id -Force
}
Start-Sleep -Seconds 1

$still = Get-NetTCPConnection -State Listen -LocalPort $port
if ($still) {
    Write-Host ("警告：端口 {0} 仍被占用，可能还有别的程序在用它。" -f $port)
} else {
    Write-Host "已停止。"
}

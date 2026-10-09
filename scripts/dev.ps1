<#
.SYNOPSIS
    一键启动后端(FastAPI)+ 前端(Vite)开发服务。

.EXAMPLE
    pwsh -File scripts/dev.ps1
    powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 -SkipFrontend
    powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 -PythonExe 'E:\hello agents_demo\.venv\Scripts\python.exe'

.NOTES
    依赖候选顺序:backend/venv -> 仓库上级的 .venv -> PATH 里的 python,
    会先检查该解释器能否 import fastapi/langchain/langgraph。
#>

[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [switch]$SkipBackend,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot "backend"
$frontendDir = Join-Path $repoRoot "frontend"
$children = New-Object System.Collections.Generic.List[System.Diagnostics.Process]
$reported = New-Object System.Collections.Generic.HashSet[int]

function Resolve-Python {
    param([string]$Explicit)

    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit)) {
            throw "指定的 Python 不存在: $Explicit"
        }
        return (Resolve-Path -LiteralPath $Explicit).Path
    }

    $candidates = @(
        (Join-Path $backendDir "venv\Scripts\python.exe"),
        (Join-Path $repoRoot ".venv\Scripts\python.exe"),
        (Join-Path $repoRoot "..\.venv\Scripts\python.exe"),
        (Join-Path $backendDir ".venv\Scripts\python.exe")
    )

    foreach ($candidate in $candidates) {
        $full = [System.IO.Path]::GetFullPath($candidate)
        if (-not (Test-Path -LiteralPath $full)) { continue }

        # 注意:Windows PowerShell 5.1 在 $ErrorActionPreference='Stop' 时会把
        # 原生命令的 stderr 当成终止错误,所以这里要单独静默掉所有输出流。
        $probeExitCode = & {
            $ErrorActionPreference = "SilentlyContinue"
            & $full -c "import fastapi, uvicorn, langchain, langgraph" *> $null
            $LASTEXITCODE
        }
        if ($probeExitCode -eq 0) { return $full }
    }

    $fallback = Get-Command python -ErrorAction SilentlyContinue
    if ($fallback) { return $fallback.Source }

    throw "找不到可用的 Python 环境。请先创建虚拟环境并 pip install -r backend/requirements.txt,或用 -PythonExe 显式指定。"
}

function Resolve-Npm {
    # 优先 npm.cmd:它可以直接被 CreateProcess 启动。
    # 某些环境(如通过 scoop/volta 安装)只有 npm.ps1 / npm 脚本,
    # 这时统一交给 cmd.exe 调用。
    $npmCmd = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
    if ($npmCmd) {
        return @{ File = $npmCmd.Source; UseCmdWrapper = $false }
    }

    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        throw "找不到 npm,请先安装 Node.js 18+。"
    }
    return @{ File = $env:ComSpec; UseCmdWrapper = $true }
}

function Assert-EnvFile {
    param([string]$Directory, [string]$Name)

    $envFile = Join-Path $Directory ".env"
    if (-not (Test-Path -LiteralPath $envFile)) {
        Write-Host "[warn] $Name 缺少 .env,正在从 .env.example 复制(记得填真实值)。" -ForegroundColor Yellow
        Copy-Item -LiteralPath (Join-Path $Directory ".env.example") -Destination $envFile
    }
}

function Start-ChildService {
    <#
        这里刻意不用 Start-Process:
        Windows PowerShell 5.1 在进程环境块同时存在 "PATH" 与 "Path" 时,
        Start-Process -NoNewWindow 会抛
        "Item has already been added. Key in dictionary: 'PATH'"。
        改用 ProcessStartInfo + UseShellExecute=$false,子进程直接继承当前
        环境与标准输出,日志仍然打印在同一个控制台里。
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string]$Arguments,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $Arguments
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $false

    return [System.Diagnostics.Process]::Start($startInfo)
}

Write-Host "仓库根目录: $repoRoot" -ForegroundColor Cyan

try {
    if (-not $SkipBackend) {
        Assert-EnvFile -Directory $backendDir -Name "backend"
        $python = Resolve-Python -Explicit $PythonExe
        Write-Host "后端 Python: $python" -ForegroundColor Green
        Write-Host "后端地址:   http://localhost:8000/docs" -ForegroundColor Green

        $children.Add((Start-ChildService -FilePath $python `
            -Arguments "-X utf8 run.py" `
            -WorkingDirectory $backendDir))
    }

    if (-not $SkipFrontend) {
        Assert-EnvFile -Directory $frontendDir -Name "frontend"
        $npm = Resolve-Npm
        $npmArgs = if ($npm.UseCmdWrapper) { "/d /c npm run dev" } else { "run dev" }
        Write-Host "前端:       http://localhost:5173" -ForegroundColor Green

        if (-not (Test-Path -LiteralPath (Join-Path $frontendDir "node_modules"))) {
            Write-Host "[info] 首次运行,先执行 npm install ..." -ForegroundColor Yellow
            if ($npm.UseCmdWrapper) {
                & $env:ComSpec /d /c "npm install --prefix `"$frontendDir`""
            }
            else {
                & $npm.File install --prefix $frontendDir
            }
        }

        $children.Add((Start-ChildService -FilePath $npm.File `
            -Arguments $npmArgs `
            -WorkingDirectory $frontendDir))
    }

    if ($children.Count -eq 0) {
        Write-Host "没有需要启动的服务(检查 -SkipBackend / -SkipFrontend)。" -ForegroundColor Yellow
        return
    }

    Write-Host "`n服务已启动,按 Ctrl+C 结束。`n" -ForegroundColor Cyan

    while ($true) {
        Start-Sleep -Seconds 2

        $aliveCount = 0
        foreach ($child in $children) {
            if ($child.HasExited) {
                if (-not $reported.Contains($child.Id)) {
                    [void]$reported.Add($child.Id)
                    Write-Host "[warn] 进程 $($child.Id) 已退出(退出码 $($child.ExitCode))。" -ForegroundColor Yellow
                }
            }
            else {
                $aliveCount++
            }
        }

        # 子进程全部退出后不再空转(否则只剩一个刷警告的死循环)
        if ($aliveCount -eq 0) {
            Write-Host "所有服务都已退出。" -ForegroundColor Yellow
            break
        }
    }
}
finally {
    foreach ($child in $children) {
        if (-not $child.HasExited) {
            Write-Host "正在停止进程 $($child.Id) ..." -ForegroundColor Yellow
            Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

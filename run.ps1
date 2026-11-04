#requires -Version 7.0
# STOCKIFY Windows localhost launcher.
#
#   .\run.ps1 -BuildEngine   build + test the C++ engine (stockify._engine), then exit
#   .\run.ps1 -Daemon        API + web + STOCKIFY market daemon
#   .\run.ps1 -DaemonOnly    STOCKIFY market daemon only

[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8001,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 3000,
    [switch]$Fresh,
    [switch]$InstallWeb,
    [switch]$ApiOnly,
    [switch]$WebOnly,
    [switch]$GeoMonitor,
    [switch]$MonitorOnly,
    [switch]$Daemon,
    [switch]$DaemonOnly,
    [switch]$BuildEngine
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$PyExe = Join-Path $Venv "Scripts\python.exe"
$Pip = Join-Path $Venv "Scripts\pip.exe"
$Stamp = Join-Path $Venv ".deps_installed"
$Req = Join-Path $Root "requirements.txt"
$WebDir = Join-Path $Root "web"
$MonitorScript = Join-Path $Root "scripts\run_geo_monitor.ps1"
$DaemonScript = Join-Path $Root "scripts\run_stockify_daemon.ps1"
$EngineDir = Join-Path $Root "engine"
$EnginePreset = "win-msvc-release"

if ($ApiOnly -and $WebOnly) {
    throw "-ApiOnly and -WebOnly cannot be used together."
}
$onlyCount = @($ApiOnly, $WebOnly, $MonitorOnly, $DaemonOnly | Where-Object { $_ }).Count
if (($MonitorOnly -or $DaemonOnly) -and $onlyCount -gt 1) {
    throw "-MonitorOnly and -DaemonOnly cannot be combined with another -*Only switch."
}

$StartApi = -not $WebOnly -and -not $MonitorOnly -and -not $DaemonOnly -and -not $BuildEngine
$StartWeb = -not $ApiOnly -and -not $MonitorOnly -and -not $DaemonOnly -and -not $BuildEngine
$StartMonitor = ($GeoMonitor -or $MonitorOnly) -and -not $BuildEngine
$StartDaemon = ($Daemon -or $DaemonOnly) -and -not $BuildEngine
$NeedPython = $StartApi -or $StartMonitor -or $StartDaemon -or $BuildEngine

function Test-SupportedPython([string]$Executable) {
    try {
        $version = & $Executable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        return $LASTEXITCODE -eq 0 -and $version -in @("3.11", "3.12")
    } catch {
        return $false
    }
}

function Find-BootstrapPython {
    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($minor in @("3.12", "3.11")) {
            $candidate = & $launcher.Source "-$minor" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-SupportedPython $candidate)) {
                return $candidate.Trim()
            }
        }
    }
    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($python -and (Test-SupportedPython $python.Source)) {
        return $python.Source
    }
    throw "Python 3.11 or 3.12 x64 is required. Install it from python.org, then rerun."
}

function Assert-NodeToolchain {
    $node = Get-Command "node.exe" -ErrorAction SilentlyContinue
    $npm = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
    if (-not $node -or -not $npm) {
        throw "Node.js and npm are required. Install Node.js 20.9 or newer."
    }
    $raw = & $node.Source -p "process.versions.node"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read the installed Node.js version."
    }
    $installed = [version]$raw.Trim()
    if ($installed -lt [version]"20.9.0") {
        throw "Node.js 20.9 or newer is required; found $installed."
    }
    return $npm.Source
}

function Stop-ProcessTree([System.Diagnostics.Process]$Process) {
    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
}

function Enter-MsvcShell {
    # Loads the MSVC x64 toolchain (cl, cmake, ninja) into this session.
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) {
        throw "vswhere.exe not found. Install Visual Studio Build Tools with the 'Desktop development with C++' workload."
    }
    $vsPath = & $vswhere -latest -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $vsPath) {
        throw ("MSVC C++ tools not found. Add them with: " +
            "& `"${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\setup.exe`" modify " +
            "--installPath <BuildTools path> --add Microsoft.VisualStudio.Workload.VCTools " +
            "--add Microsoft.VisualStudio.Component.VC.CMake.Project --includeRecommended --passive")
    }
    $devShell = Join-Path $vsPath "Common7\Tools\Microsoft.VisualStudio.DevShell.dll"
    Import-Module $devShell
    Enter-VsDevShell -VsInstallPath $vsPath -SkipAutomaticLocation `
        -DevCmdArguments "-arch=x64 -host_arch=x64" | Out-Null
}

function Assert-EngineTools {
    $cmake = Get-Command "cmake.exe" -ErrorAction SilentlyContinue
    if (-not $cmake) {
        throw "cmake not found. Add the 'C++ CMake tools for Windows' component to Visual Studio Build Tools."
    }
    $raw = ((& $cmake.Source --version) | Select-Object -First 1) -replace '[^0-9.]', ''
    if ([version]$raw -lt [version]"3.25") {
        throw "CMake 3.25 or newer is required; found $raw."
    }
    if (-not (Get-Command "ninja.exe" -ErrorAction SilentlyContinue)) {
        throw "ninja not found. Add the 'C++ CMake tools for Windows' component to Visual Studio Build Tools."
    }
    & $PyExe -c "import pybind11" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "pybind11 is missing from .venv. Run: .\.venv\Scripts\pip.exe install -r requirements.txt"
    }
}

function Invoke-EngineBuild {
    Enter-MsvcShell
    Assert-EngineTools
    $pybindDir = (& $PyExe -m pybind11 --cmakedir).Trim()
    Push-Location $EngineDir
    try {
        Write-Host "Configuring engine ($EnginePreset)" -ForegroundColor Cyan
        & cmake --preset $EnginePreset "-Dpybind11_DIR=$pybindDir" "-DPython_EXECUTABLE=$PyExe"
        if ($LASTEXITCODE -ne 0) { throw "CMake configure failed." }
        Write-Host "Building engine" -ForegroundColor Cyan
        & cmake --build --preset $EnginePreset
        if ($LASTEXITCODE -ne 0) { throw "Engine build failed." }
        Write-Host "Running engine tests" -ForegroundColor Cyan
        & ctest --preset $EnginePreset
        if ($LASTEXITCODE -ne 0) { throw "Engine tests failed." }
    } finally {
        Pop-Location
    }
    & $PyExe -m stockify engine
    if ($LASTEXITCODE -ne 0) { throw "stockify._engine does not import." }
}

function Test-EngineFresh {
    # Warns (never fails) when the engine module is missing or older than its sources.
    $module = Get-ChildItem -Path (Join-Path $Root "stockify") -Filter "_engine*.pyd" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if (-not $module) {
        Write-Warning "STOCKIFY engine is not built. Run .\run.ps1 -BuildEngine."
        return
    }
    $newest = Get-ChildItem -Path $EngineDir -Recurse -File -Include *.cpp, *.hpp, *.h, *.in, CMakeLists.txt, CMakePresets.json |
        Where-Object { $_.FullName -notlike "*\build\*" } |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($newest -and $newest.LastWriteTimeUtc -gt $module.LastWriteTimeUtc) {
        Write-Warning "STOCKIFY engine is older than engine/ sources. Run .\run.ps1 -BuildEngine."
    }
}

Set-Location $Root

if ($NeedPython) {
    if ($Fresh -and (Test-Path $Venv)) {
        Write-Host "Removing existing .venv" -ForegroundColor Yellow
        Remove-Item -Recurse -Force $Venv
    }
    if (-not (Test-Path $PyExe)) {
        $bootstrap = Find-BootstrapPython
        Write-Host "Creating Python environment with $bootstrap" -ForegroundColor Cyan
        & $bootstrap -m venv $Venv
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $PyExe)) {
            throw "Failed to create .venv."
        }
    }
    if (-not (Test-SupportedPython $PyExe)) {
        throw "The existing .venv is not Python 3.11 or 3.12. Run .\run.ps1 -Fresh."
    }
    $needInstall = $Fresh -or -not (Test-Path $Stamp) -or `
        ((Get-Item $Req).LastWriteTimeUtc -gt (Get-Item $Stamp).LastWriteTimeUtc)
    if ($needInstall) {
        Write-Host "Installing Python dependencies" -ForegroundColor Cyan
        & $PyExe -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
        & $Pip install -r $Req
        if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }
        New-Item -ItemType File -Path $Stamp -Force | Out-Null
    }
}

if ($BuildEngine) {
    Invoke-EngineBuild
    Write-Host "Engine built." -ForegroundColor Green
    exit 0
}
Test-EngineFresh

$Npm = $null
if ($StartWeb) {
    $Npm = Assert-NodeToolchain
    if (-not (Test-Path $WebDir)) {
        throw "web directory not found at $WebDir."
    }
    if ($InstallWeb -or -not (Test-Path (Join-Path $WebDir "node_modules"))) {
        Write-Host "Installing web dependencies with npm ci" -ForegroundColor Cyan
        Push-Location $WebDir
        try {
            & $Npm ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed." }
        } finally {
            Pop-Location
        }
    }
}

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Write-Warning "Root .env is missing; credentialed features will be unavailable."
}

$env:STOCKIFY_API_ORIGIN = "http://127.0.0.1:$ApiPort"
$env:STOCKIFY_WEB_ORIGIN = "http://127.0.0.1:$WebPort"
$children = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()

try {
    if ($StartApi) {
        Write-Host "Starting API at $env:STOCKIFY_API_ORIGIN" -ForegroundColor Cyan
        $process = Start-Process -FilePath $PyExe -ArgumentList @(
            "-m", "uvicorn", "server.main:app", "--reload",
            "--host", "127.0.0.1", "--port", "$ApiPort"
        ) -WorkingDirectory $Root -NoNewWindow -PassThru
        $children.Add($process)
    }
    if ($StartWeb) {
        Write-Host "Starting web at $env:STOCKIFY_WEB_ORIGIN" -ForegroundColor Magenta
        $process = Start-Process -FilePath $Npm -ArgumentList @(
            "run", "dev", "--", "--hostname", "127.0.0.1", "--port", "$WebPort"
        ) -WorkingDirectory $WebDir -NoNewWindow -PassThru
        $children.Add($process)
    }
    if ($StartMonitor) {
        if (-not (Test-Path $MonitorScript)) {
            throw "Geo monitor launcher not found at $MonitorScript."
        }
        Write-Host "Starting geo monitor" -ForegroundColor Green
        $process = Start-Process -FilePath (Get-Command "pwsh.exe").Source -ArgumentList @(
            "-NoProfile", "-File", $MonitorScript
        ) -WorkingDirectory $Root -NoNewWindow -PassThru
        $children.Add($process)
    }
    if ($StartDaemon) {
        if (-not (Test-Path $DaemonScript)) {
            throw "STOCKIFY daemon launcher not found at $DaemonScript."
        }
        Write-Host "Starting STOCKIFY daemon" -ForegroundColor Green
        $process = Start-Process -FilePath (Get-Command "pwsh.exe").Source -ArgumentList @(
            "-NoProfile", "-File", $DaemonScript
        ) -WorkingDirectory $Root -NoNewWindow -PassThru
        $children.Add($process)
    }

    Write-Host ""
    Write-Host "STOCKIFY is running. Press Ctrl+C to stop." -ForegroundColor White
    while ($true) {
        foreach ($child in $children) {
            if ($child.HasExited) {
                throw "Child process $($child.Id) exited with code $($child.ExitCode)."
            }
        }
        Start-Sleep -Milliseconds 500
    }
} finally {
    foreach ($child in $children) {
        Stop-ProcessTree $child
    }
    Write-Host "STOCKIFY stopped." -ForegroundColor Yellow
}

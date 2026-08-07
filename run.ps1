# STOCKIFY launcher — FastAPI backend (:8001) + Next.js frontend (:3000)
#
# Usage:  .\run.ps1                          # both servers, default ports
#         .\run.ps1 -ApiPort 8001 -WebPort 3000
#         .\run.ps1 -Fresh                   # recreate .venv + reinstall Python deps
#         .\run.ps1 -InstallWeb              # also re-run npm install in web/
#         .\run.ps1 -ApiOnly                 # FastAPI only
#         .\run.ps1 -WebOnly                 # Next.js only
#
# This replaces the original Streamlit launcher (deleted in Phase 6 of the
# overhaul). It is identical in behaviour to `run-dev.ps1`, kept under the
# original filename so existing muscle-memory still works.

param(
    [int]$ApiPort = 8001,
    [int]$WebPort = 3000,
    [switch]$Fresh,
    [switch]$InstallWeb,
    [switch]$ApiOnly,
    [switch]$WebOnly
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$Venv  = Join-Path $Root ".venv"
$PyExe = Join-Path $Venv "Scripts\python.exe"
$Pip   = Join-Path $Venv "Scripts\pip.exe"
$Stamp = Join-Path $Venv ".deps_installed"
$Req   = Join-Path $Root "requirements.txt"
$WebDir = Join-Path $Root "web"

# ── Python venv + deps (skip if WebOnly) ───────────────────────────────────────
if (-not $WebOnly) {
    if ($Fresh -and (Test-Path $Venv)) {
        Write-Host ">> Removing existing .venv" -ForegroundColor Yellow
        Remove-Item -Recurse -Force $Venv
    }

    if (-not (Test-Path $PyExe)) {
        Write-Host ">> Creating venv at .venv" -ForegroundColor Cyan
        python -m venv $Venv
        if (-not (Test-Path $PyExe)) { throw "Failed to create venv. Is Python on PATH?" }
    }

    $needInstall = $Fresh -or -not (Test-Path $Stamp) -or `
                   ((Get-Item $Req).LastWriteTime -gt (Get-Item $Stamp).LastWriteTime)
    if ($needInstall) {
        Write-Host ">> Installing Python requirements" -ForegroundColor Cyan
        & $PyExe -m pip install --upgrade pip
        & $Pip install -r $Req
        if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
        New-Item -ItemType File -Path $Stamp -Force | Out-Null
        if (Select-String -Path $Req -Pattern "^playwright" -Quiet) {
            Write-Host ">> Installing Playwright Chromium" -ForegroundColor Cyan
            & $PyExe -m playwright install chromium
        }
    }
}

# ── Web deps (skip if ApiOnly) ─────────────────────────────────────────────────
if (-not $ApiOnly) {
    if (-not (Test-Path $WebDir)) {
        throw "web/ directory not found at $WebDir. Run 'npx create-next-app web ...' first."
    }
    if ($InstallWeb -or -not (Test-Path (Join-Path $WebDir "node_modules"))) {
        Write-Host ">> Installing web/ npm dependencies" -ForegroundColor Cyan
        Push-Location $WebDir
        try { npm install } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    }
}

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Write-Warning "Project secrets file not found - app will run but API-key-dependent features will fail."
}

$env:STOCKIFY_API_ORIGIN = "http://127.0.0.1:$ApiPort"

# ── Boot ───────────────────────────────────────────────────────────────────────
$jobs = @()

if (-not $WebOnly) {
    Write-Host ">> Starting FastAPI on http://localhost:$ApiPort" -ForegroundColor Green
    $apiJob = Start-Job -Name "stockify-api" -ScriptBlock {
        param($Root, $PyExe, $Port)
        Set-Location $Root
        & $PyExe -m uvicorn server.main:app --reload --host 127.0.0.1 --port $Port
    } -ArgumentList $Root, $PyExe, $ApiPort
    $jobs += $apiJob
}

if (-not $ApiOnly) {
    Write-Host ">> Starting Next.js on http://localhost:$WebPort" -ForegroundColor Green
    $webJob = Start-Job -Name "stockify-web" -ScriptBlock {
        param($WebDir, $Port, $ApiOrigin)
        Set-Location $WebDir
        $env:STOCKIFY_API_ORIGIN = $ApiOrigin
        npm run dev -- --port $Port
    } -ArgumentList $WebDir, $WebPort, $env:STOCKIFY_API_ORIGIN
    $jobs += $webJob
}

Write-Host ""
Write-Host "──────────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host " STOCKIFY — Ctrl+C to stop both servers"                    -ForegroundColor White
if (-not $WebOnly) { Write-Host "  API   http://localhost:$ApiPort/api/health" -ForegroundColor Cyan }
if (-not $ApiOnly) { Write-Host "  Web   http://localhost:$WebPort"            -ForegroundColor Cyan }
Write-Host "──────────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

try {
    while ($true) {
        foreach ($j in $jobs) {
            Receive-Job -Job $j -Keep:$false | ForEach-Object {
                $tag = if ($j.Name -eq "stockify-api") { "API" } else { "WEB" }
                $color = if ($j.Name -eq "stockify-api") { "Cyan" } else { "Magenta" }
                Write-Host "[$tag] $_" -ForegroundColor $color
            }
            if ($j.State -ne "Running") {
                Write-Host "[!]  Job $($j.Name) exited with state $($j.State)" -ForegroundColor Red
                throw "child server crashed"
            }
        }
        Start-Sleep -Milliseconds 250
    }
}
finally {
    foreach ($j in $jobs) {
        try { Stop-Job -Job $j -ErrorAction SilentlyContinue } catch {}
        try { Remove-Job -Job $j -Force -ErrorAction SilentlyContinue } catch {}
    }
    Write-Host ">> Stopped." -ForegroundColor Yellow
}

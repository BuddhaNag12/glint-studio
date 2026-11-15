<#
.SYNOPSIS
    Starts the local Glint Studio API bridge (uvicorn) and an HTTPS tunnel
    (Cloudflare Tunnel, Tailscale Funnel, or ngrok).
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgeDir = Split-Path -Parent $ScriptDir
$EnvFile = Join-Path $BridgeDir '.env'
$VenvPython = Join-Path $BridgeDir '.venv\Scripts\python.exe'

function Read-DotEnv {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $val = $trimmed.Substring($idx + 1).Trim()
        if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
            if ($val.Length -ge 2) { $val = $val.Substring(1, $val.Length - 2) }
        }
        $map[$key] = $val
    }
    return $map
}

function Find-Binary {
    param([string]$Name, [string[]]$ExtraPaths)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in $ExtraPaths) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Find-Cloudflared {
    Find-Binary -Name 'cloudflared' -ExtraPaths @(
        'C:\Program Files (x86)\cloudflared\cloudflared.exe',
        'C:\Program Files\cloudflared\cloudflared.exe',
        (Join-Path $env:LOCALAPPDATA 'cloudflared\cloudflared.exe')
    )
}

function Find-Tailscale {
    Find-Binary -Name 'tailscale' -ExtraPaths @(
        'C:\Program Files\Tailscale\tailscale.exe',
        'C:\Program Files (x86)\Tailscale\tailscale.exe'
    )
}

function Find-Ngrok {
    $wingetPkgExe = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\Ngrok*') -Filter 'ngrok.exe' -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    Find-Binary -Name 'ngrok' -ExtraPaths @(
        (Join-Path $env:LOCALAPPDATA 'ngrok\ngrok.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\ngrok.exe'),
        $wingetPkgExe,
        'C:\Program Files\ngrok\ngrok.exe'
    )
}

function Get-TailscaleUrl {
    param([string]$TailscaleExe)
    try {
        $raw = & $TailscaleExe status --json 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) {
            $parsed = $raw | ConvertFrom-Json
            $dnsName = [string]$parsed.Self.DNSName
            if ($dnsName) {
                return "https://$($dnsName.TrimEnd('.'))"
            }
        }
    } catch {}
    return $null
}

function Test-PortListening {
    param([string]$HostName, [int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($HostName, $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(250)) {
            $client.EndConnect($iar)
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Show-TunnelReady {
    param([string]$LocalUrl, [string]$PublicUrl, [string]$Note)
    if ($PublicUrl) {
        try { Set-Clipboard -Value $PublicUrl } catch {}
    }
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Green
    Write-Host "  Local Bridge : $LocalUrl" -ForegroundColor Green
    if ($PublicUrl) {
        Write-Host "  Public URL   : $PublicUrl" -ForegroundColor Yellow
        Write-Host '  (Public URL copied to clipboard!)' -ForegroundColor Cyan
        Write-Host '  Point your client's base URL at this address.' -ForegroundColor Gray
    } elseif ($Note) {
        Write-Host "  Status       : $Note" -ForegroundColor Green
    }
    Write-Host '  Press Ctrl+C or close this window to stop both.' -ForegroundColor Gray
    Write-Host '============================================================' -ForegroundColor Green
    Write-Host ''
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  Glint Studio bridge + tunnel launcher' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Python virtualenv not found at '$VenvPython'. Run 'python -m venv .venv' and 'pip install -e .[dev]' inside '$BridgeDir'."
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Missing '$EnvFile'. Copy '.env.example' to '.env' and configure API_KEY before starting."
}

$cfg = Read-DotEnv -Path $EnvFile
$bridgeHost = if ($cfg['BRIDGE_HOST']) { $cfg['BRIDGE_HOST'] } else { '127.0.0.1' }
$bridgePort = if ($cfg['BRIDGE_PORT']) { [int]$cfg['BRIDGE_PORT'] } else { 8000 }
$comfyUrl = if ($cfg['COMFYUI_URL']) { $cfg['COMFYUI_URL'].TrimEnd('/') } else { 'http://127.0.0.1:8188' }
$tunnelProvider = if ($cfg['TUNNEL_PROVIDER']) { $cfg['TUNNEL_PROVIDER'].Trim().ToLowerInvariant() } else { 'auto' }
$tunnelToken = if ($cfg['CLOUDFLARE_TUNNEL_TOKEN']) { $cfg['CLOUDFLARE_TUNNEL_TOKEN'].Trim() } else { '' }
$tunnelName = if ($cfg['CLOUDFLARE_TUNNEL_NAME']) { $cfg['CLOUDFLARE_TUNNEL_NAME'].Trim() } else { '' }
$ngrokDomain = if ($cfg['NGROK_DOMAIN']) { ($cfg['NGROK_DOMAIN'].Trim() -replace '^https?://', '').TrimEnd('/') } else { '' }
$localBridgeUrl = "http://${bridgeHost}:${bridgePort}"

$cloudflaredExe = Find-Cloudflared
$tailscaleExe = Find-Tailscale
$ngrokExe = Find-Ngrok

if ($tunnelProvider -eq 'auto') {
    if ($tunnelToken -or $tunnelName) {
        $tunnelProvider = 'cloudflare'
    } elseif ($ngrokDomain) {
        $tunnelProvider = 'ngrok'
    } elseif ($cloudflaredExe) {
        $tunnelProvider = 'cloudflare'
    } elseif ($tailscaleExe) {
        $tunnelProvider = 'tailscale'
    } elseif ($ngrokExe) {
        $tunnelProvider = 'ngrok'
    } else {
        throw 'No tunnel CLI found (cloudflared, tailscale, or ngrok). Install one or set TUNNEL_PROVIDER=none.'
    }
}

switch ($tunnelProvider) {
    'cloudflare' {
        if (-not $cloudflaredExe) { throw 'TUNNEL_PROVIDER=cloudflare selected, but cloudflared.exe was not found.' }
    }
    'tailscale' {
        if (-not $tailscaleExe) { throw 'TUNNEL_PROVIDER=tailscale selected, but tailscale.exe was not found.' }
    }
    'ngrok' {
        if (-not $ngrokExe) { throw 'TUNNEL_PROVIDER=ngrok selected, but ngrok.exe was not found.' }
    }
    'none' {}
    default {
        throw "Unsupported TUNNEL_PROVIDER '$tunnelProvider'. Use: auto, cloudflare, tailscale, ngrok, or none."
    }
}

try {
    $comfyUri = [Uri]$comfyUrl
    if (Test-PortListening -HostName $comfyUri.Host -Port $comfyUri.Port) {
        Write-Host "[OK] ComfyUI detected at $comfyUrl" -ForegroundColor Green
    } else {
        Write-Host "[WARN] ComfyUI is not listening at $comfyUrl yet. Start ComfyUI Desktop before generating images." -ForegroundColor Yellow
    }
} catch {
    Write-Host "[INFO] ComfyUI URL: $comfyUrl" -ForegroundColor Gray
}

$uvicornProc = $null
$tunnelProc = $null
$uvicornOutLog = Join-Path $env:TEMP 'glint-studio-uvicorn.out.log'
$uvicornErrLog = Join-Path $env:TEMP 'glint-studio-uvicorn.err.log'
$tunnelErrLog = Join-Path $env:TEMP 'glint-studio-tunnel.err.log'
$tunnelOutLog = Join-Path $env:TEMP 'glint-studio-tunnel.out.log'

try {
    if (Test-PortListening -HostName $bridgeHost -Port $bridgePort) {
        Write-Host "[WARN] Port $bridgePort is already active at $localBridgeUrl; keeping existing listener." -ForegroundColor Yellow
    } else {
        Remove-Item -LiteralPath $uvicornOutLog, $uvicornErrLog -ErrorAction SilentlyContinue
        Write-Host "[1/2] Starting Python FastAPI bridge at $localBridgeUrl ..." -ForegroundColor White
        $uvicornProc = Start-Process -FilePath $VenvPython `
            -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', $bridgeHost, '--port', "$bridgePort") `
            -WorkingDirectory $BridgeDir `
            -RedirectStandardOutput $uvicornOutLog `
            -RedirectStandardError $uvicornErrLog `
            -NoNewWindow `
            -PassThru

        $deadline = (Get-Date).AddSeconds(20)
        $ready = $false
        while ((Get-Date) -lt $deadline) {
            if ($uvicornProc.HasExited) {
                $errText = if (Test-Path -LiteralPath $uvicornErrLog) { Get-Content -LiteralPath $uvicornErrLog -Raw } else { '' }
                throw "uvicorn exited early (code $($uvicornProc.ExitCode)).`n$errText"
            }
            if (Test-PortListening -HostName $bridgeHost -Port $bridgePort) {
                $ready = $true
                break
            }
            Start-Sleep -Milliseconds 300
        }
        if (-not $ready) {
            throw "Timed out waiting for uvicorn to listen on $localBridgeUrl."
        }
        Write-Host "[OK] Python bridge is listening on $localBridgeUrl" -ForegroundColor Green
    }

    if ($tunnelProvider -eq 'none') {
        Show-TunnelReady -LocalUrl $localBridgeUrl -PublicUrl $null -Note 'Local-only mode (TUNNEL_PROVIDER=none)'
        while ($true) {
            if ($uvicornProc -and $uvicornProc.HasExited) {
                Write-Host "[ERROR] uvicorn stopped unexpectedly (code $($uvicornProc.ExitCode))." -ForegroundColor Red
                if (Test-Path -LiteralPath $uvicornErrLog) { Get-Content -LiteralPath $uvicornErrLog -Tail 30 }
                break
            }
            Start-Sleep -Milliseconds 500
        }
        return
    }

    Remove-Item -LiteralPath $tunnelErrLog, $tunnelOutLog -ErrorAction SilentlyContinue
    $tunnelExe = $null
    $tunnelArgs = @()
    $urlPattern = ''
    $staticNote = ''

    if ($tunnelProvider -eq 'cloudflare') {
        $tunnelExe = $cloudflaredExe
        if ($tunnelToken) {
            Write-Host '[2/2] Starting named Cloudflare Tunnel via CLOUDFLARE_TUNNEL_TOKEN ...' -ForegroundColor White
            $tunnelArgs = @('tunnel', '--no-autoupdate', 'run', '--token', $tunnelToken)
            $staticNote = 'Named Cloudflare Tunnel active'
        } elseif ($tunnelName) {
            Write-Host "[2/2] Starting named Cloudflare Tunnel '$tunnelName' ..." -ForegroundColor White
            $tunnelArgs = @('tunnel', '--no-autoupdate', 'run', $tunnelName)
            $staticNote = "Named Cloudflare Tunnel '$tunnelName' active"
        } else {
            Write-Host "[2/2] Starting Cloudflare Quick Tunnel -> $localBridgeUrl ..." -ForegroundColor White
            $tunnelArgs = @('tunnel', '--no-autoupdate', '--url', $localBridgeUrl)
            $urlPattern = 'https://[a-zA-Z0-9-]+\.trycloudflare\.com'
        }
    } elseif ($tunnelProvider -eq 'tailscale') {
        $tunnelExe = $tailscaleExe
        Write-Host "[2/2] Starting Tailscale Funnel -> $localBridgeUrl ..." -ForegroundColor White
        $tunnelArgs = @('funnel', $localBridgeUrl)
        $urlPattern = 'https://[a-zA-Z0-9.-]+\.ts\.net'
    } elseif ($tunnelProvider -eq 'ngrok') {
        $tunnelExe = $ngrokExe
        if ($ngrokDomain) {
            Write-Host "[2/2] Starting ngrok static domain https://$ngrokDomain -> $localBridgeUrl ..." -ForegroundColor White
            $tunnelArgs = @('http', "--domain=$ngrokDomain", $localBridgeUrl, '--log=stdout')
        } else {
            Write-Host "[2/2] Starting ngrok tunnel -> $localBridgeUrl ..." -ForegroundColor White
            $tunnelArgs = @('http', $localBridgeUrl, '--log=stdout')
        }
        $urlPattern = 'https://[a-zA-Z0-9.-]+\.ngrok[a-zA-Z0-9.-]*'
    }

    $tunnelProc = Start-Process -FilePath $tunnelExe `
        -ArgumentList $tunnelArgs `
        -WorkingDirectory $BridgeDir `
        -RedirectStandardOutput $tunnelOutLog `
        -RedirectStandardError $tunnelErrLog `
        -NoNewWindow `
        -PassThru

    $announced = $false
    $waitUntil = (Get-Date).AddSeconds(25)
    while (-not $tunnelProc.HasExited) {
        if (-not $announced) {
            if ($staticNote) {
                $announced = $true
                Show-TunnelReady -LocalUrl $localBridgeUrl -PublicUrl $null -Note $staticNote
            } else {
                $combined = ''
                if (Test-Path -LiteralPath $tunnelOutLog) { $combined += (Get-Content -LiteralPath $tunnelOutLog -Raw -ErrorAction SilentlyContinue) + "`n" }
                if (Test-Path -LiteralPath $tunnelErrLog) { $combined += (Get-Content -LiteralPath $tunnelErrLog -Raw -ErrorAction SilentlyContinue) }
                if ($combined -match $urlPattern) {
                    $announced = $true
                    Show-TunnelReady -LocalUrl $localBridgeUrl -PublicUrl $Matches[0] -Note ''
                } elseif ($tunnelProvider -eq 'tailscale' -and ((Get-Date) -gt $waitUntil.AddSeconds(-20))) {
                    $tsUrl = Get-TailscaleUrl -TailscaleExe $tailscaleExe
                    if ($tsUrl) {
                        $announced = $true
                        Show-TunnelReady -LocalUrl $localBridgeUrl -PublicUrl $tsUrl -Note ''
                    }
                } elseif ((Get-Date) -gt $waitUntil) {
                    $announced = $true
                    Write-Host "[INFO] $tunnelProvider tunnel running (URL not captured yet; logs follow):" -ForegroundColor Yellow
                    if ($combined.Trim()) { Write-Host $combined }
                }
            }
        }

        if ($uvicornProc -and $uvicornProc.HasExited) {
            Write-Host "[ERROR] uvicorn stopped unexpectedly (code $($uvicornProc.ExitCode))." -ForegroundColor Red
            if (Test-Path -LiteralPath $uvicornErrLog) {
                Get-Content -LiteralPath $uvicornErrLog -Tail 30
            }
            break
        }
        Start-Sleep -Milliseconds 400
    }

    if ($tunnelProc -and $tunnelProc.HasExited) {
        Write-Host "[ERROR] $tunnelProvider exited (code $($tunnelProc.ExitCode))." -ForegroundColor Red
        if (Test-Path -LiteralPath $tunnelErrLog) { Get-Content -LiteralPath $tunnelErrLog -Tail 30 }
        if (Test-Path -LiteralPath $tunnelOutLog) { Get-Content -LiteralPath $tunnelOutLog -Tail 30 }
    }
} finally {
    Write-Host ''
    Write-Host 'Shutting down services...' -ForegroundColor Yellow
    if ($tunnelProc -and -not $tunnelProc.HasExited) {
        Stop-Process -Id $tunnelProc.Id -Force -ErrorAction SilentlyContinue
    }
    if ($uvicornProc -and -not $uvicornProc.HasExited) {
        Stop-Process -Id $uvicornProc.Id -Force -ErrorAction SilentlyContinue
    }
}

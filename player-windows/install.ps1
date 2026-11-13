# install.ps1 - Vitrail Player Windows
# Run as Administrator in PowerShell

param(
  [string]$InstallDir = "C:\Program Files\SignFlow Player"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Vitrail Player - Windows Installer" -ForegroundColor Cyan
Write-Host "Install directory: $InstallDir"
Write-Host ""

# 0. Node.js 22+ prerequisite (Electron 42 declares engines node >= 22.12).
#    Same contract as player-linux / player-rpi install.sh, which both install Node 22 LTS
#    themselves. Windows was the only platform NOT doing it: a fresh Windows 11 has no node,
#    so the `npm install` below died with a bare "npm is not recognized" (measured 2026-08-09
#    on a clean Win11 25H2 VM, which is exactly the "existing PC" this channel targets).
$nodeOk = $false
if (Get-Command node -ErrorAction SilentlyContinue) {
  $major = 0
  try { $major = [int](& node -e "console.log(process.versions.node.split('.')[0])" 2>$null) } catch {}
  if ($major -ge 22) {
    $nodeOk = $true
    Write-Host "[prereq] Node.js $(& node --version) already present." -ForegroundColor Green
  } else {
    Write-Host "[prereq] Node.js $major is too old (22+ required)." -ForegroundColor Yellow
  }
}
if (-not $nodeOk) {
  Write-Host "[prereq] Installing Node.js 22+ LTS..." -ForegroundColor Yellow
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id OpenJS.NodeJS.LTS -e --silent `
      --accept-source-agreements --accept-package-agreements --disable-interactivity | Out-Null
    # The installer updates the MACHINE Path, but NOT the Path of this already-running
    # process. Without this refresh the `npm install` below still fails, right after a
    # successful install - the confusing failure mode this whole block exists to prevent.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
  }
  if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw ("Node.js 20+ is required and could not be installed automatically. " +
           "Install it from https://nodejs.org/en/download (LTS, x64) and run this script again.")
  }
  Write-Host "[prereq] Node.js $(& node --version) installed." -ForegroundColor Green
}

# 1. Copy files
if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Recurse -Force "$ScriptDir\*" $InstallDir
Write-Host "[1/5] Files copied" -ForegroundColor Green

# 2. npm install
Push-Location $InstallDir
npm install --omit=dev --silent
if (-not $?) { throw "npm install failed" }

# Ensure electron directory exists before writing path.txt
$electronDir = "$InstallDir\node_modules\electron"
if (Test-Path $electronDir) {
  $pathTxt = "$electronDir\path.txt"
  if (-not (Test-Path $pathTxt) -or (Get-Content $pathTxt -Raw).Trim() -eq "") {
    [System.IO.File]::WriteAllText($pathTxt, "electron.exe")
  }

  # Download binary if missing
  $electronExe = "$electronDir\dist\electron.exe"
  if (-not (Test-Path $electronExe)) {
    $ver = (Get-Content "$electronDir\package.json" | ConvertFrom-Json).version
    $url = "https://github.com/electron/electron/releases/download/v$ver/electron-v$ver-win32-x64.zip"
    $zip = "$env:TEMP\electron-$ver.zip"
    Write-Host "  Downloading Electron v$ver..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri $url -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath "$electronDir\dist" -Force
    [System.IO.File]::WriteAllText($pathTxt, "electron.exe")
  }
}

Pop-Location
Write-Host "[2/5] npm dependencies installed" -ForegroundColor Green

# 3. Startup wrapper (hidden PowerShell window — no cmd.exe visible)
Write-Host "[3/5] Startup script created" -ForegroundColor Green

# 4. Scheduled Task (at logon, restart on failure, no time limit, hidden window)
$taskName = "SignFlow Player"
# Log OS borné : le redirect capte le stderr natif Electron/Chromium = croissance sans limite
# sinon (vu 144 Mo sur un NUC). Pas de logrotate sous Windows → on TRONQUE le fichier au lancement
# s'il dépasse 10 Mo (vérifié à chaque (re)démarrage de la tâche : boot + relances RestartCount).
# Le logger applicatif interne (userData\logs\player.log, 2 Mo×2) reste la trace persistante bornée.
$logPath   = Join-Path $env:USERPROFILE 'signflow-player.log'
$launchCmd = "if ((Test-Path '$logPath') -and (Get-Item '$logPath').Length -gt 10MB) { Set-Content -LiteralPath '$logPath' -Value '' }; Set-Location '$InstallDir'; node_modules\.bin\electron.cmd . 2>&1 | Out-File -LiteralPath '$logPath' -Append"
$action    = New-ScheduledTaskAction `
  -Execute "PowerShell.exe" `
  -Argument "-WindowStyle Hidden -NonInteractive -Command `"$launchCmd`""
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
  -ExecutionTimeLimit 0 `
  -RestartCount 99 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
  -TaskName $taskName `
  -Action   $action `
  -Trigger  $trigger `
  -Settings $settings `
  -RunLevel Highest `
  -Force | Out-Null
Write-Host "[4/5] Scheduled Task registered (at logon, auto-restart)" -ForegroundColor Green

# 5. Hide last username at login screen
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v DontDisplayLastUserName /t REG_DWORD /d 1 /f | Out-Null

# 6. Kiosk power settings - disable screen sleep + screensaver
powercfg /change monitor-timeout-ac 0
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d "0" /f | Out-Null
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveTimeOut /t REG_SZ /d "0" /f | Out-Null
Write-Host "[6/7] Power settings configured (no sleep, no screensaver)" -ForegroundColor Green

# 7. Auto-hide taskbar (prevents it from overlapping the kiosk window)
$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StuckRects3"
if (Test-Path $regPath) {
  $val = (Get-ItemProperty $regPath).Settings
  # Byte 8 controls auto-hide: OR with 0x01 to enable auto-hide
  $val[8] = $val[8] -bor 0x01
  Set-ItemProperty -Path $regPath -Name Settings -Value $val
}
Write-Host "[7/7] Taskbar set to auto-hide" -ForegroundColor Green

# 8. Firewall rule - allow inbound UDP for inter-player broadcasts:
#    7778 = LAN sync (§III.3), 7779 = VOD multi-target trigger (Feature B Phase 3).
#    Without 7779 a fresh Windows player cannot RECEIVE a LAN VOD trigger.
$fwRuleName = "SignFlow Player LAN Sync"
if (-not (Get-NetFirewallRule -DisplayName $fwRuleName -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $fwRuleName `
    -Direction Inbound `
    -Protocol UDP `
    -LocalPort 7778,7779 `
    -Action Allow `
    -Profile Any | Out-Null
}
Write-Host "[8/8] Firewall rule for LAN sync + VOD trigger (UDP 7778,7779) configured" -ForegroundColor Green

# HW decode detection - D3D11 (MediaFoundation/DXVA) is active by default in
# Chromium on Windows. Writes capabilities.json (read + reported by the player).
# Override: set env var SF_HW_DECODE = auto | on | off before running.
Write-Host "[hw-decode] Detecting hardware video decode..." -ForegroundColor Cyan
$sfHwDecode = $env:SF_HW_DECODE
if (-not $sfHwDecode) { $sfHwDecode = "auto" }
$gpuVendor = "unknown"
$gpuModel = ""
try {
  $vc = Get-CimInstance Win32_VideoController -ErrorAction Stop | Select-Object -First 1
  if ($vc) {
    $gpuModel = "$($vc.Name)"
    $compat = "$($vc.AdapterCompatibility) $($vc.Name)"
    if ($compat -match "Intel") { $gpuVendor = "intel" }
    elseif ($compat -match "AMD|Advanced Micro|ATI|Radeon") { $gpuVendor = "amd" }
    elseif ($compat -match "NVIDIA|GeForce") { $gpuVendor = "nvidia" }
  }
} catch {}
# « Microsoft Basic Display Adapter » = pilote GPU réel absent -> pas de décodage HW.
$basic = ($gpuModel -match "Basic Display") -or ($compat -match "Microsoft Basic Display") -or ($gpuVendor -eq "unknown")
$hwMethod = "d3d11"
$hwActive = $true
if ($basic) { $hwMethod = "none"; $hwActive = $false }
if ($sfHwDecode -eq "off") { $hwMethod = "none"; $hwActive = $false }

# userData Electron = %APPDATA%\<package.json name> (windows suffixe en
# signflow-player-windows) -> sans ca le player ne trouve pas capabilities.json.
$pkgName = "signflow-player-windows"
try { $pkgName = (Get-Content (Join-Path $InstallDir "package.json") -Raw | ConvertFrom-Json).name } catch {}
$capDir = Join-Path $env:APPDATA $pkgName
New-Item -ItemType Directory -Force -Path $capDir | Out-Null
$caps = [ordered]@{
  gpu_vendor       = $gpuVendor
  gpu_model        = $gpuModel
  hw_decode        = $hwMethod
  hw_decode_active = $hwActive
  codecs           = @("h264", "hevc", "vp9")
  gl_backend       = "angle"
  sf_hw_decode     = $sfHwDecode
  chromium_flags   = @()
  detection_source = "install"
  detected_at      = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}
$capJson = $caps | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText((Join-Path $capDir "capabilities.json"), $capJson)
Write-Host "[hw-decode] $gpuVendor | method=$hwMethod active=$hwActive" -ForegroundColor Green

Write-Host ""
Write-Host "Installation complete!" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Launch the player to run the setup wizard:"
Write-Host "     Start-ScheduledTask -TaskName '$taskName'"
Write-Host "  2. OR pre-configure manually:"
Write-Host "     %APPDATA%\signflow-player-windows\config.json"
Write-Host '     {"backend_url":"http://192.168.1.10:8080","org_token":"sfp_...","player_name":"windows-player-01"}'

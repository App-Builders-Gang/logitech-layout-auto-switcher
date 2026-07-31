<#
.SYNOPSIS
    Logitech Layout Auto Switcher installer for Windows.

.DESCRIPTION
    One-liner (nothing cloned):
        irm https://raw.githubusercontent.com/App-Builders-Gang/logitech-layout-auto-switcher/main/install.ps1 | iex

    From a checkout:
        .\install.ps1

    To pass options through the one-liner, set an environment variable first:
        $env:LOGISWITCH_OS = 'macos'; irm https://.../install.ps1 | iex

    Creates a virtualenv, installs the package, verifies the keyboard is
    reachable and registers a Scheduled Task that starts the agent at logon.
    No administrator rights are required.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Uninstall
    .\install.ps1 -TargetOs macos
#>
# No [CmdletBinding()] and no [ValidateSet] here on purpose: when this script is
# piped through `iex`, parameter attributes are applied to the caller's scope and
# an empty default would be rejected before the body ever runs. The target OS is
# validated explicitly below instead.
param(
    [switch]$Uninstall,
    [string]$TargetOs = $env:LOGISWITCH_OS,
    [string]$InstallDir = $(if ($env:LOGISWITCH_HOME) { $env:LOGISWITCH_HOME } else { Join-Path $env:LOCALAPPDATA 'LogiSwitch' }),
    [string]$Ref = $(if ($env:LOGISWITCH_REF) { $env:LOGISWITCH_REF } else { 'main' })
)

$ErrorActionPreference = 'Stop'
$Repo = 'App-Builders-Gang/logitech-layout-auto-switcher'

$ValidTargets = @('windows', 'macos', 'linux', 'android', 'ios', 'chrome',
                  'win', 'mac', 'pc', 'osx', 'darwin', 'chromeos')
if ($TargetOs) {
    $TargetOs = $TargetOs.Trim().ToLower()
    if ($ValidTargets -notcontains $TargetOs) {
        throw "Unknown target OS '$TargetOs'. Choose one of: $($ValidTargets -join ', ')"
    }
}

function Write-Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "    $m" -ForegroundColor Yellow }

if ($env:LOGISWITCH_UNINSTALL -eq '1') { $Uninstall = $true }

# --- where are we running from? ----------------------------------------------
# Piped through `iex`, there is no script file and no checkout around us.
$ScriptDir = $null
if ($PSCommandPath) { $ScriptDir = Split-Path -Parent $PSCommandPath }

if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir 'pyproject.toml'))) {
    $Root = $ScriptDir
    $FromCheckout = $true
    $Venv = Join-Path $Root '.venv'
} else {
    $Root = Join-Path $InstallDir 'app'
    $FromCheckout = $false
    $Venv = Join-Path $InstallDir 'venv'
}
$Python = Join-Path $Venv 'Scripts\python.exe'

function Resolve-BasePython {
    foreach ($candidate in @('py', 'python3', 'python')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        # Skip the Microsoft Store stub, which exits without running anything.
        if ($command.Source -like '*WindowsApps*') { continue }
        $prefix = if ($candidate -eq 'py') { @('-3') } else { @() }
        $version = & $command.Source @($prefix + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])')) 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -match '^(\d+)\.(\d+)$') {
            if ([int]$Matches[1] -ge 3 -and [int]$Matches[2] -ge 9) {
                return @{ Path = $command.Source; Prefix = $prefix; Version = $version }
            }
        }
    }
    throw "Python 3.9 or newer was not found. Install it from https://python.org (tick 'Add to PATH') and re-run."
}

# --- uninstall ----------------------------------------------------------------
if ($Uninstall) {
    Write-Step 'Removing the logon agent'
    if (Test-Path $Python) {
        & $Python -m logiswitch uninstall
    } else {
        foreach ($task in @('LogiSwitch', 'MXSwitch')) {
            schtasks /Query /TN $task *> $null
            if ($LASTEXITCODE -eq 0) {
                schtasks /End /TN $task *> $null
                schtasks /Delete /TN $task /F *> $null
                Write-Ok "removed scheduled task '$task'"
            }
        }
    }
    if (-not $FromCheckout -and (Test-Path $InstallDir)) {
        Remove-Item -Recurse -Force $InstallDir
        Write-Ok "removed $InstallDir"
    }
    Write-Ok 'Done.'
    # schtasks leaves a non-zero code behind when it queried a task that was not
    # there; do not let that read as a failed uninstall.
    $global:LASTEXITCODE = 0
    return
}

Write-Step "Detected Windows $([Environment]::OSVersion.Version) ($env:PROCESSOR_ARCHITECTURE)"

Write-Step 'Locating Python'
$base = Resolve-BasePython
Write-Ok "$($base.Path) ($($base.Version))"

# --- fetch, if we were piped in ----------------------------------------------
if (-not $FromCheckout) {
    Write-Step "Downloading $Repo@$Ref"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    if (Test-Path $Root) { Remove-Item -Recurse -Force $Root }
    $zip = Join-Path ([IO.Path]::GetTempPath()) "logiswitch-$Ref.zip"
    $extract = Join-Path ([IO.Path]::GetTempPath()) "logiswitch-extract-$([guid]::NewGuid().ToString('N'))"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "https://codeload.github.com/$Repo/zip/$Ref" -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $extract -Force
        $inner = Get-ChildItem -Path $extract -Directory | Select-Object -First 1
        if (-not $inner) { throw 'the downloaded archive was empty' }
        Move-Item -Path $inner.FullName -Destination $Root
    } finally {
        Remove-Item -Force $zip -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path (Join-Path $Root 'pyproject.toml'))) { throw 'download did not contain the project' }
    Write-Ok $Root
}

# --- install ------------------------------------------------------------------
if (-not (Test-Path $Python)) {
    Write-Step 'Creating the virtualenv'
    & $base.Path @($base.Prefix + @('-m', 'venv', $Venv))
    if ($LASTEXITCODE -ne 0) { throw 'failed to create the virtualenv' }
}

Write-Step 'Installing logiswitch'
& $Python -m pip install --quiet --upgrade pip
& $Python -m pip install --quiet "$Root"
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
Write-Ok (& $Python -m logiswitch --version)

Write-Step 'Checking the keyboard is reachable'
& $Python -m logiswitch status
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 'No supported device answered.'
    Write-Warn2 'If you use a KVM, switch it to this machine and re-run.'
    Write-Warn2 'Installing anyway; the agent retries on every device event.'
}

Write-Step 'Registering the logon agent'
$installArgs = @('-m', 'logiswitch', 'install')
if ($TargetOs) { $installArgs += @('--os', $TargetOs) }
& $Python @installArgs
if ($LASTEXITCODE -ne 0) { throw 'service registration failed' }

Write-Host ''
Write-Host 'Installed.' -ForegroundColor Green
Write-Host "  status:    $Python -m logiswitch status"
Write-Host "  service:   $Python -m logiswitch service-status"
Write-Host "  logs:      $env:LOCALAPPDATA\LogiSwitch\logiswitch.log"
if ($FromCheckout) {
    Write-Host "  uninstall: .\install.ps1 -Uninstall"
} else {
    Write-Host "  uninstall: `$env:LOGISWITCH_UNINSTALL='1'; irm https://raw.githubusercontent.com/$Repo/main/install.ps1 | iex"
}
$global:LASTEXITCODE = 0

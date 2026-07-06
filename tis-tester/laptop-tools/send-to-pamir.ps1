<#
send-to-pamir.ps1 - push tis-tester.tar.gz to PAMIR over the USB-C serial console.

USAGE (PowerShell, in the folder containing tis-tester.tar.gz):
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\send-to-pamir.ps1 -Port COM5

BEFORE RUNNING:
  * Find your COM port in Device Manager -> Ports (COM & LPT).
  * Log in to PAMIR once with PuTTY on that port (1500000 baud), then CLOSE
    PuTTY *without typing exit* - the shell stays alive on the serial line
    and this script talks to it. (If you do hit a login prompt, the script
    will ask for your username/password and log in for you.)
  * Only one program can hold a COM port at a time: PuTTY must be closed.
#>

param(
    [Parameter(Mandatory=$true)][string]$Port,
    [string]$File = ".\tis-tester.tar.gz",
    [int]$Baud = 1500000
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $File)) {
    Write-Host "ERROR: $File not found. Run this from the folder containing tis-tester.tar.gz" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- serial io
$sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, ([System.IO.Ports.Parity]::None), 8, ([System.IO.Ports.StopBits]::One)
$sp.NewLine     = "`n"
$sp.DtrEnable   = $true
$sp.RtsEnable   = $true
$sp.ReadTimeout = 500
try { $sp.Open() } catch {
    Write-Host "ERROR: could not open $Port (is PuTTY still running? wrong port?)" -ForegroundColor Red
    exit 1
}

function Drain { try { [void]$sp.ReadExisting() } catch {} }

function SendLine([string]$s) { $sp.Write($s + "`n") }

function WaitFor([string]$needle, [int]$timeoutSec = 8) {
    $buf = ""
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 120
        try { $buf += $sp.ReadExisting() } catch {}
        if ($buf -match [regex]::Escape($needle)) { return $buf }
    }
    return $buf   # caller checks
}

Write-Host "== Waking the serial console on $Port ==" -ForegroundColor Cyan
Drain
SendLine ""
Start-Sleep -Milliseconds 600
$out = ""
try { $out = $sp.ReadExisting() } catch {}

# -------------------------------------------------------------- auto-login
if ($out -match "login:") {
    $user = Read-Host "PAMIR username"
    SendLine $user
    $out = WaitFor "assword" 6
    if ($out -match "assword") {
        $pass = Read-Host "PAMIR password" -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                 [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pass))
        SendLine $plain
        Start-Sleep -Seconds 2
        Drain
    }
}

# ------------------------------------------------------- verify live shell
Drain
SendLine "echo __SHELL__`$((6*7))__"
$out = WaitFor "__SHELL__42__" 6
if ($out -notmatch "__SHELL__42__") {
    Write-Host "ERROR: no shell prompt answered on $Port." -ForegroundColor Red
    Write-Host "Open PuTTY on $Port at $Baud, log in, close PuTTY (don't type exit), retry."
    $sp.Close(); exit 1
}
Write-Host "Shell is alive." -ForegroundColor Green

# ------------------------------------------------------------ transfer file
$bytes  = [IO.File]::ReadAllBytes((Resolve-Path $File))
$b64    = [Convert]::ToBase64String($bytes)
$lines  = [regex]::Matches($b64, ".{1,40}") | ForEach-Object { $_.Value }
$sha    = (Get-FileHash $File -Algorithm SHA256).Hash.ToLower()
Write-Host ("Sending {0:N0} bytes ({1} acknowledged chunks)..." -f `
            $bytes.Length, $lines.Count)

Drain
SendLine "stty -echo"
Start-Sleep -Milliseconds 300
SendLine ": > /tmp/tis-tester.b64"
SendLine "echo __TRANSFER_READY__"
$out = WaitFor "__TRANSFER_READY__" 5
if ($out -notmatch "__TRANSFER_READY__") {
    Write-Host "ERROR: board did not initialize the transfer." -ForegroundColor Red
    $sp.Close(); exit 1
}

$i = 0
foreach ($ln in $lines) {
    # Keep every serial command well below the bridge's practical line limit.
    SendLine ("printf '%s' '" + $ln + "' >> /tmp/tis-tester.b64")
    $i++
    $marker = "__CHUNK_${i}__"
    SendLine ("echo " + $marker)
    $ack = WaitFor $marker 5
    if ($ack -notmatch [regex]::Escape($marker)) {
        SendLine "stty echo"
        Write-Host "ERROR: serial acknowledgement lost at chunk $i." -ForegroundColor Red
        Write-Host "The partial file is harmless; rerun this script to restart cleanly."
        $sp.Close(); exit 1
    }
    if (($i % 25 -eq 0) -or ($i -eq $lines.Count)) {
        Write-Progress -Activity "Transferring over serial" `
                       -PercentComplete ([int](100 * $i / $lines.Count))
    }
}
SendLine "base64 -d /tmp/tis-tester.b64 > /tmp/tis-tester.tar.gz && rm -f /tmp/tis-tester.b64"
$out = WaitFor "root@" 8
SendLine "stty echo"
Write-Progress -Activity "Transferring over serial" -Completed

# ------------------------------------------------------------------ verify
Drain
SendLine "sha256sum /tmp/tis-tester.tar.gz"
$out = WaitFor $sha 10
if ($out -notmatch $sha) {
    Write-Host "ERROR: checksum mismatch - line noise during transfer." -ForegroundColor Red
    Write-Host "Just run the script again (it overwrites and re-verifies)."
    $sp.Close(); exit 1
}
Write-Host "Checksum verified: $sha" -ForegroundColor Green

# ----------------------------------------------------------------- unpack
SendLine 'mv /tmp/tis-tester.tar.gz ~/ && mkdir -p ~/tis-tester && tar xzf ~/tis-tester.tar.gz -C ~/tis-tester && echo __UNPACK_OK__'
$out = WaitFor "__UNPACK_OK__" 10
$sp.Close()

if ($out -match "__UNPACK_OK__") {
    Write-Host ""
    Write-Host "SUCCESS - tis-tester is unpacked in ~/tis-tester on PAMIR." -ForegroundColor Green
    Write-Host "Now reopen PuTTY on $Port and continue with SERIAL_SETUP.md Part 4:"
    Write-Host "    cd ~/tis-tester"
    Write-Host "    nix develop"
    Write-Host "    tis-test interactive --mock"
} else {
    Write-Host "Transfer verified but unpack didn't confirm - reopen PuTTY and run:" -ForegroundColor Yellow
    Write-Host '    mv /tmp/tis-tester.tar.gz ~/ ; mkdir -p ~/tis-tester ; tar xzf ~/tis-tester.tar.gz -C ~/tis-tester'
}

<#
deploy/warp-connect.ps1 - enroll this box in the ae0rm Zero Trust org and
connect WARP, so 10.30.0.20 (skywave) becomes reachable.

Two moments need a human: the installer's UAC prompt (if WARP is not yet
installed) and the browser login the enrollment opens at the org's Access
page. Everything else is scripted and idempotent.

Usage: .\deploy\warp-connect.ps1 [-Org ae0rm] [-InstallIfMissing]
#>
param([string]$Org = "ae0rm", [switch]$InstallIfMissing)

$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "[warp] $m" }

$cli = "C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe"
if (-not (Test-Path $cli)) {
    if (-not $InstallIfMissing) { throw "warp-cli not found at $cli; run with -InstallIfMissing (raises a UAC prompt)" }
    Step "installing Cloudflare WARP via winget (accept the UAC prompt)"
    winget install --id Cloudflare.Warp --source winget --accept-package-agreements --accept-source-agreements --silent
    if (-not (Test-Path $cli)) { throw "install finished but $cli is still missing" }
}
Step "warp-cli $(& $cli --version 2>&1 | Select-Object -First 1)"

$status = (& $cli status 2>&1) -join " "
Step "status: $status"

$reg = (& $cli registration show 2>&1) -join " "
if ($reg -match $Org) {
    Step "already registered with $Org"
} else {
    if ($reg -match "Registered|Device ID") {
        Step "a different registration exists; removing it first"
        & $cli --accept-tos registration delete | Out-Null
    }
    $help = (& $cli --help 2>&1) -join " "
    if ($help -match "teams-enroll") {
        Step "enrolling with teams-enroll $Org - complete the login in the browser that opens"
        & $cli --accept-tos teams-enroll $Org
    } else {
        Step "enrolling with registration new $Org - complete the login in the browser that opens"
        & $cli --accept-tos registration new $Org
    }
    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 5
        $reg = (& $cli registration show 2>&1) -join " "
    } until (($reg -match $Org) -or ((Get-Date) -gt $deadline))
    if ($reg -notmatch $Org) { throw "enrollment did not complete within 10 minutes: $reg" }
    Step "registered with $Org"
}

& $cli --accept-tos connect | Out-Null
$deadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Seconds 3
    $status = (& $cli status 2>&1) -join " "
} until (($status -match "Connected") -or ((Get-Date) -gt $deadline))
Step "status: $status"
if ($status -notmatch "Connected") { throw "WARP did not reach Connected" }

Step "probing skywave 10.30.0.20:22"
$t = Test-NetConnection -ComputerName 10.30.0.20 -Port 22 -WarningAction SilentlyContinue
if ($t.TcpTestSucceeded) { Step "skywave reachable - run deploy\run-skywave.ps1" } else { throw "WARP connected but 10.30.0.20:22 is not reachable; check the org's split-tunnel / private network route for 10.30.0.0/24" }

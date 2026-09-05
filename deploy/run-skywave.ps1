<#
deploy/run-skywave.ps1 — drive the Skywave runbook from the Windows operator seat.

Prerequisites on this box: Cloudflare WARP connected to the ae0rm org (see
deploy/warp-connect.ps1), an SSH key authorized for nick@skywave, and a
`Host debain1` entry in ~/.ssh/config. The script stops with a clear message
at the first prerequisite that is missing.

Usage:  .\deploy\run-skywave.ps1              # deploy + receipts
        .\deploy\run-skywave.ps1 -DeployOnly
        .\deploy\run-skywave.ps1 -ReceiptsOnly
#>
param([switch]$DeployOnly, [switch]$ReceiptsOnly, [string]$SshHost = "debain1")

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

function Step($m) { Write-Host "[run-skywave] $m" }

Step "preflight: ssh $SshHost"
$probe = ssh -o BatchMode=yes -o ConnectTimeout=10 $SshHost 'echo ok-$(hostname)' 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host $probe
    throw "cannot reach $SshHost over SSH. Is WARP connected (warp-cli status) and is ~/.ssh/id_ed25519.pub in nick's authorized_keys on skywave?"
}
Step "reached $probe"

Step "copying deploy/skywave to the host"
ssh $SshHost 'mkdir -p ~/grid-deploy'
scp -q "$repo\deploy\skywave\deploy.sh" "$repo\deploy\skywave\receipts.sh" "$repo\deploy\skywave\bus_capture.py" "${SshHost}:~/grid-deploy/"
ssh $SshHost 'chmod +x ~/grid-deploy/*.sh; sed -i "s/\r$//" ~/grid-deploy/*.sh ~/grid-deploy/*.py'

if (-not $ReceiptsOnly) {
    Step "running deploy.sh (idempotent)"
    ssh -t $SshHost 'bash ~/grid-deploy/deploy.sh'
    if ($LASTEXITCODE -ne 0) { throw "deploy.sh exited $LASTEXITCODE — read the last [deploy] FAIL line above" }
}

if (-not $DeployOnly) {
    Step "collecting receipts (about 12 minutes)"
    $out = ssh $SshHost 'bash ~/grid-deploy/receipts.sh' 2>&1 | Tee-Object -Variable log | Select-Object -Last 1
    $local = Join-Path $repo "deploy\receipts\$(Split-Path -Leaf $out)"
    New-Item -ItemType Directory -Force $local | Out-Null
    scp -q -r "${SshHost}:$out/*" $local
    Step "receipts copied to $local"
    Get-ChildItem $local | Select-Object -ExpandProperty Name
}

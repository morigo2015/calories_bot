[CmdletBinding()]
param(
    [string]$SshHost = "hetzner",
    [string]$RemotePath = "/home/igor/calories-bot",
    [string]$ServiceName = "calories-bot"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $projectRoot

try {
    Write-Host "Copying application files to ${SshHost}:$RemotePath ..."

    & ssh $SshHost "mkdir -p '$RemotePath' '$RemotePath/data/photos'"
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the VPS directory." }

    & scp -r "calories_bot" "requirements.txt" "${SshHost}:$RemotePath/"
    if ($LASTEXITCODE -ne 0) { throw "Could not copy application files to the VPS." }

    $prepareCommand = "set -e; cd '$RemotePath'; test -f calories_bot/main.py; test -f requirements.txt; test -x .venv/bin/python; .venv/bin/python -m pip install -r requirements.txt; PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m compileall -q calories_bot"
    & ssh $SshHost $prepareCommand
    if ($LASTEXITCODE -ne 0) { throw "VPS preparation failed." }

    Write-Host "Restarting $ServiceName ..."
    $restartCommand = "sudo systemctl restart $ServiceName; deploy_code=`$?; sudo systemctl status --no-pager $ServiceName || true; sudo journalctl -u $ServiceName -n 20 --no-pager; exit `$deploy_code"
    & ssh -t $SshHost $restartCommand
    if ($LASTEXITCODE -ne 0) { throw "Service restart failed." }

    Write-Host "Deployment completed."
}
finally {
    Pop-Location
}

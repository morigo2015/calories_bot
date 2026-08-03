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
    if (-not (Test-Path -LiteralPath ".git")) {
        throw "Git is not initialized. Run automation\init-git.ps1 first."
    }

    $pendingChanges = (& git status --porcelain)
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect the Git working tree." }
    if ($pendingChanges) {
        throw "The working tree is not clean. Save the version before deployment."
    }

    $branch = [string](& git branch --show-current)
    $branch = $branch.Trim()
    if ([string]::IsNullOrWhiteSpace($branch)) {
        throw "Could not determine the current Git branch."
    }

    $remotes = @(& git remote)
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect Git remotes." }
    if ($remotes -notcontains "origin") {
        throw "Remote 'origin' is not configured."
    }

    & git fetch origin $branch
    if ($LASTEXITCODE -ne 0) { throw "Could not fetch origin/$branch." }

    $localCommit = [string](& git rev-parse HEAD)
    $remoteCommit = [string](& git rev-parse "origin/$branch")
    $localCommit = $localCommit.Trim()
    $remoteCommit = $remoteCommit.Trim()
    if ($localCommit -ne $remoteCommit) {
        throw "Local HEAD is not equal to origin/$branch. Push the saved version first."
    }

    $copyItems = @(
        "calories_bot",
        "scripts",
        "automation",
        "deploy",
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "README.md",
        "PRD.md",
        ".env.example"
    )

    & ssh $SshHost "mkdir -p '$RemotePath' '$RemotePath/data/photos'"
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the VPS directory." }

    & scp -r @copyItems "${SshHost}:$RemotePath/"
    if ($LASTEXITCODE -ne 0) { throw "Could not copy project files to the VPS." }

    $prepareCommand = "set -e; cd '$RemotePath'; test -x .venv/bin/python; .venv/bin/python -m pip install -r requirements.txt; PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m compileall -q calories_bot"
    & ssh $SshHost $prepareCommand
    if ($LASTEXITCODE -ne 0) {
        throw "VPS dependency installation or compile check failed."
    }

    $restartCommand = "sudo systemctl restart $ServiceName; deploy_code=`$?; sudo systemctl status --no-pager $ServiceName || true; sudo journalctl -u $ServiceName -n 30 --no-pager; exit `$deploy_code"
    & ssh -t $SshHost $restartCommand
    if ($LASTEXITCODE -ne 0) { throw "Service restart failed." }

    Write-Host "Deployed commit $localCommit to ${SshHost}:$RemotePath."
}
finally {
    Pop-Location
}

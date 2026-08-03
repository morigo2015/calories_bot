[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RemoteUrl,

    [string]$Branch = "main"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $projectRoot

try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git is not installed or is not available in PATH."
    }

    $userName = [string](& git config --get user.name)
    $userEmail = [string](& git config --get user.email)
    $userName = $userName.Trim()
    $userEmail = $userEmail.Trim()
    if ([string]::IsNullOrWhiteSpace($userName) -or [string]::IsNullOrWhiteSpace($userEmail)) {
        throw "Configure Git identity first: git config --global user.name 'Your Name' and git config --global user.email 'you@example.com'."
    }

    if (-not (Test-Path -LiteralPath ".git")) {
        & git init
        if ($LASTEXITCODE -ne 0) { throw "git init failed." }

        & git checkout -B $Branch
        if ($LASTEXITCODE -ne 0) { throw "Could not create branch '$Branch'." }
    }
    else {
        $currentBranch = [string](& git branch --show-current)
        $currentBranch = $currentBranch.Trim()
        if ($currentBranch -ne $Branch) {
            throw "Current branch is '$currentBranch', expected '$Branch'."
        }
    }

    $remotes = @(& git remote)
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect Git remotes." }

    if ($remotes -notcontains "origin") {
        & git remote add origin $RemoteUrl
        if ($LASTEXITCODE -ne 0) { throw "Could not add remote 'origin'." }
    }
    else {
        $origin = [string](& git remote get-url origin)
        if ($LASTEXITCODE -ne 0) { throw "Could not read remote 'origin'." }
        $origin = $origin.Trim()
        if ($origin -ne $RemoteUrl) {
            throw "Remote 'origin' already points to '$origin'."
        }
    }

    & git add -A
    if ($LASTEXITCODE -ne 0) { throw "git add failed." }

    & git diff --cached --quiet
    $diffExitCode = $LASTEXITCODE
    if ($diffExitCode -eq 1) {
        & git commit -m "Initial commit"
        if ($LASTEXITCODE -ne 0) { throw "Initial commit failed." }
    }
    elseif ($diffExitCode -gt 1) {
        throw "Could not inspect staged changes."
    }

    & git push -u origin $Branch
    if ($LASTEXITCODE -ne 0) { throw "Initial push failed." }

    Write-Host "Git repository initialized and pushed to origin/$Branch."
}
finally {
    Pop-Location
}

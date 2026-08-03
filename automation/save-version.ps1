[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Message,

    [switch]$LocalOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $projectRoot

try {
    if (-not (Test-Path -LiteralPath ".git")) {
        throw "Git is not initialized. Run automation\init-git.ps1 first."
    }

    & git add -A
    if ($LASTEXITCODE -ne 0) { throw "git add failed." }

    & git diff --cached --quiet
    $diffExitCode = $LASTEXITCODE
    if ($diffExitCode -eq 1) {
        & git commit -m $Message
        if ($LASTEXITCODE -ne 0) { throw "Commit failed." }
    }
    elseif ($diffExitCode -eq 0) {
        Write-Host "No new changes to commit."
    }
    else {
        throw "Could not inspect staged changes."
    }

    if (-not $LocalOnly) {
        $branch = [string](& git branch --show-current)
        $branch = $branch.Trim()
        if ([string]::IsNullOrWhiteSpace($branch)) {
            throw "Could not determine the current Git branch."
        }

        & git push -u origin $branch
        if ($LASTEXITCODE -ne 0) { throw "Push failed." }
        Write-Host "Version saved locally and pushed to origin/$branch."
    }
    else {
        Write-Host "Version saved locally. Push was skipped."
    }
}
finally {
    Pop-Location
}

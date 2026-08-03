[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Message,

    [switch]$LocalOnly,
    [switch]$SkipChecks
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
Push-Location $projectRoot

try {
    if (-not (Test-Path -LiteralPath ".git")) {
        throw "Git is not initialized. Run automation\init-git.ps1 first."
    }

    if (-not $SkipChecks) {
        if (-not (Test-Path -LiteralPath $python)) {
            throw "Virtual environment not found at $python."
        }

        & $python -m ruff format --check .
        if ($LASTEXITCODE -ne 0) { throw "Ruff format check failed." }

        & $python -m ruff check .
        if ($LASTEXITCODE -ne 0) { throw "Ruff lint failed." }

        & $python -m mypy calories_bot
        if ($LASTEXITCODE -ne 0) { throw "Mypy failed." }

        & $python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
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

        $remotes = @(& git remote)
        if ($LASTEXITCODE -ne 0) { throw "Could not inspect Git remotes." }
        if ($remotes -notcontains "origin") {
            throw "Remote 'origin' is not configured."
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

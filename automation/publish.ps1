[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Message
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "1/2 Saving version to GitHub ..."
& (Join-Path $PSScriptRoot "save-version.ps1") -Message $Message

Write-Host "2/2 Updating VPS ..."
& (Join-Path $PSScriptRoot "deploy-vps.ps1")

Write-Host "Version saved and VPS updated."

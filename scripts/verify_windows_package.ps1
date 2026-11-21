# Windows-only artifact checks. Never launches installer, publishes or edits tags.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Bundle', 'Installer')]
    [string]$Stage
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Real Windows artifact verification requires Windows; do not substitute Linux output.'
}
$Root = Split-Path -Parent $PSScriptRoot
$Version = & python (Join-Path $PSScriptRoot 'check_release.py')
if ($LASTEXITCODE -ne 0) { throw 'Release input check failed.' }
$Version = $Version.Trim()

function Require-NonemptyFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing artifact: $Path" }
    if ((Get-Item -LiteralPath $Path).Length -le 0) { throw "Empty artifact: $Path" }
}

$Bundle = Join-Path $Root 'dist/uniter-server/uniter-server.exe'
Require-NonemptyFile $Bundle
if ($Stage -eq 'Installer') {
    $Installer = Join-Path $Root 'dist/UniterSetup.exe'
    $Upload = Join-Path $Root 'installer/Output/UniterSetup.exe'
    Require-NonemptyFile $Installer
    Require-NonemptyFile $Upload
    $Info = [Diagnostics.FileVersionInfo]::GetVersionInfo($Installer)
    $Expected = "$Version.0"
    $FileVersion = "$($Info.FileMajorPart).$($Info.FileMinorPart).$($Info.FileBuildPart).$($Info.FilePrivatePart)"
    $ProductVersion = "$($Info.ProductMajorPart).$($Info.ProductMinorPart).$($Info.ProductBuildPart).$($Info.ProductPrivatePart)"
    if ($FileVersion -ne $Expected -or $ProductVersion -ne $Expected) {
        throw "Installer resource mismatch: file=$FileVersion product=$ProductVersion expected=$Expected"
    }
    if ($Info.ProductName -ne 'Uniter') { throw 'Unexpected installer product name.' }
    $Hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash
    if ($Hash -ne (Get-FileHash -LiteralPath $Upload -Algorithm SHA256).Hash) {
        throw 'Upload artifact differs from verified installer.'
    }
    Write-Output "Verified installer file/product $FileVersion; SHA256 $Hash"
}
Write-Output "Verified nonempty $Stage artifacts for v$Version"

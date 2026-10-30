param(
    [string]$Version = "1.11.424",
    [switch]$PreservePreviousReleases
)

$ErrorActionPreference = "Stop"

# Build the Windows execution package and the Linux portal package from the
# same source revision and version.  Keeping this as one entry point prevents
# one side of the control plane from being released without the other.
$windowsScript = Join-Path $PSScriptRoot "build_windows_plugin_package.ps1"
$linuxScript = Join-Path $PSScriptRoot "build_baota_package.ps1"
if (-not (Test-Path -LiteralPath $windowsScript -PathType Leaf)) {
    throw "Windows package builder is missing: $windowsScript"
}
if (-not (Test-Path -LiteralPath $linuxScript -PathType Leaf)) {
    throw "Linux package builder is missing: $linuxScript"
}

& $windowsScript -Version $Version -ArchiveSuffix "windows-plugin" -PreservePreviousReleases:$PreservePreviousReleases
& $linuxScript -Version $Version -ArchiveSuffix "all-in-one" -PreservePreviousReleases:$PreservePreviousReleases

$packageFolder = [string]::new([char[]](22561, 22612))
$packageRoot = Join-Path (Split-Path -Parent $PSScriptRoot) $packageFolder
if ((-not $PreservePreviousReleases) -and (Test-Path -LiteralPath $packageRoot -PathType Container)) {
    # A joint release manifest is generated below; stale manifests from older
    # versions must not remain next to the current dual-end package.
    Get-ChildItem -LiteralPath $packageRoot -Filter "Joker_Baota_Remote_v*-release.json" -File -ErrorAction SilentlyContinue |
        Remove-Item -Force
}
$windowsArchive = Join-Path $packageRoot "Joker_Baota_Remote_v$Version-windows-plugin.zip"
$linuxArchive = Join-Path $packageRoot "Joker_Baota_Remote_v$Version-all-in-one.zip"
foreach ($archive in @($windowsArchive, $linuxArchive)) {
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
        throw "Expected release archive was not created: $archive"
    }
}

$manifest = [ordered]@{
    format_version = 1
    release_version = $Version
    windows_plugin = [ordered]@{
        archive = [System.IO.Path]::GetFileName($windowsArchive)
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $windowsArchive).Hash.ToLowerInvariant()
    }
    linux_portal = [ordered]@{
        archive = [System.IO.Path]::GetFileName($linuxArchive)
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $linuxArchive).Hash.ToLowerInvariant()
    }
    synchronization = [ordered]@{
        windows_executes = $true
        ftp_database_is_validation_authority = $true
        websocket_paths = @("/api/sync/v1/ws", "/api/game/v1/ws")
    }
}
$manifestPath = Join-Path $packageRoot "Joker_Baota_Remote_v$Version-release.json"
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Get-Item -LiteralPath $windowsArchive, "$windowsArchive.sha256", $linuxArchive, "$linuxArchive.sha256", $manifestPath

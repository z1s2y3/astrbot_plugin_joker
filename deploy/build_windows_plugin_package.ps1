param(
    [string]$Version = "1.11.424",
    [string]$ArchiveSuffix = "windows-plugin",
    [switch]$PreservePreviousReleases
)

$ErrorActionPreference = "Stop"

$pluginRoot = Split-Path -Parent $PSScriptRoot
$packageFolder = [string]::new([char[]](22561, 22612))
$packageRootPath = Join-Path $pluginRoot $packageFolder
if (-not (Test-Path -LiteralPath $packageRootPath -PathType Container)) {
    New-Item -ItemType Directory -Path $packageRootPath -Force | Out-Null
}
$packageRoot = Get-Item -LiteralPath $packageRootPath
$archivePath = Join-Path $packageRoot.FullName "Joker_Baota_Remote_v$Version-$ArchiveSuffix.zip"
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "joker_windows_" + [guid]::NewGuid().ToString("N")
)
$sourceRoot = Join-Path $temporaryRoot "astrbot_plugin_joker"
$rootFiles = @(
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "feature_catalog.py",
    "runtime_compat.py",
    "requirements.txt",
    "README.md",
    "CHANGELOG.md",
    "PROJECT_STRUCTURE.md"
)
# web_client contains the shared schema and command projection used by the
# Windows executor when it applies Linux portal configuration.  It must ship
# with the Windows plugin even though the web server itself runs on Linux.
$sourceDirectories = @("auth", "qqadmin", "scum", "web_client")

try {
    New-Item -ItemType Directory -Path $sourceRoot -Force | Out-Null
    foreach ($relativePath in $rootFiles) {
        $source = Join-Path $pluginRoot $relativePath
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination $sourceRoot -Force
        }
    }
    foreach ($directory in $sourceDirectories) {
        $source = Join-Path $pluginRoot $directory
        if (Test-Path -LiteralPath $source -PathType Container) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $sourceRoot $directory) -Recurse -Force
        }
    }
    $websocketDocs = Join-Path $pluginRoot "websocket"
    if (Test-Path -LiteralPath $websocketDocs -PathType Container) {
        Copy-Item -LiteralPath $websocketDocs -Destination (Join-Path $sourceRoot "websocket") -Recurse -Force
    }

    Get-ChildItem -LiteralPath $sourceRoot -Directory -Recurse -Filter "__pycache__" |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $sourceRoot -Directory -Recurse | Where-Object {
        $_.Name -eq "plugin_data" -or $_.Name -eq "data" -or $_.Name -eq ".timed_dispatch_locks"
    } | Sort-Object FullName -Descending | Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $sourceRoot -File -Recurse | Where-Object {
        $_.Extension -in @(".pyc", ".pyo") -or $_.Name -eq ".env" -or $_.Name -like ".env.*" -or $_.Extension -in @(".db", ".sqlite", ".sqlite3")
    } | Remove-Item -Force

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $temporaryRoot,
        $archivePath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    $archive = [System.IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $entryNames = @($archive.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        $required = @(
            "astrbot_plugin_joker/main.py",
            "astrbot_plugin_joker/metadata.yaml",
            "astrbot_plugin_joker/qqadmin/im3_socketio_client.py",
            "astrbot_plugin_joker/web_client/__init__.py",
            "astrbot_plugin_joker/web_client/feature_schema.py",
            "astrbot_plugin_joker/web_client/sync_projection.py"
        )
        $missing = @($required | Where-Object { $_ -notin $entryNames })
        if ($missing.Count -gt 0) {
            throw "Required Windows plugin files missing: $($missing -join ', ')"
        }
        $hasWebSocketDoc = @(
            $entryNames | Where-Object {
                $_ -like "astrbot_plugin_joker/websocket/*.md"
            }
        ).Count -gt 0
        if (-not $hasWebSocketDoc) {
            throw "Required Windows plugin WebSocket documentation is missing"
        }
        $forbidden = @("plugin_data", "auth.json", "bindings.json", ".env", "__pycache__", ".pyc", ".db")
        $matches = @($entryNames | Where-Object {
            $entryName = $_.ToLowerInvariant()
            $forbidden | Where-Object { $entryName.Contains($_.ToLowerInvariant()) }
        })
        if ($matches.Count -gt 0) {
            throw "Runtime data found in Windows plugin package: $($matches -join ', ')"
        }
    }
    finally {
        $archive.Dispose()
    }

    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath
    $sidecarPath = "$archivePath.sha256"
    [System.IO.File]::WriteAllText(
        $sidecarPath,
        "SHA256  $($hash.Hash)  $([System.IO.Path]::GetFileName($archivePath))" + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )

    # Keep the handoff directory unambiguous.  Remove older Windows packages
    # only after the new archive and sidecar have passed all checks.
    if (-not $PreservePreviousReleases) {
        Get-ChildItem -LiteralPath $packageRoot.FullName -File -Filter "Joker_Baota_Remote_v*-windows-plugin.zip" |
            Where-Object { $_.FullName -ne $archivePath } |
            Remove-Item -Force
        Get-ChildItem -LiteralPath $packageRoot.FullName -File -Filter "Joker_Baota_Remote_v*-windows-plugin.zip.sha256" |
            Where-Object { $_.FullName -ne $sidecarPath } |
            Remove-Item -Force
    }
    Get-Item -LiteralPath $archivePath, $sidecarPath
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}

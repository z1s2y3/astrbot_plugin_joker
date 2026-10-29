param(
    [string]$Version = "1.11.414",
    [string]$ArchiveSuffix = "all-in-one",
    [switch]$PreservePreviousReleases
)

$ErrorActionPreference = "Stop"

$pluginRoot = Split-Path -Parent $PSScriptRoot
# Keep the script ASCII-only here. Windows PowerShell 5.1 may otherwise read
# a UTF-8 script without a BOM using the system code page and create a garbled
# directory name on Chinese Windows installations.
$packageFolder = [string]::new([char[]](22561, 22612))
$packageRootPath = Join-Path $pluginRoot $packageFolder
if (-not (Test-Path -LiteralPath $packageRootPath -PathType Container)) {
    New-Item -ItemType Directory -Path $packageRootPath -Force | Out-Null
}
$packageRoot = Get-Item -LiteralPath $packageRootPath
$stagingRoot = Join-Path $packageRoot.FullName "01_plugin_source\astrbot_plugin_joker"
$stagingParent = Split-Path -Parent $stagingRoot
$archivePath = Join-Path $packageRoot.FullName "Joker_Baota_Remote_v$Version-$ArchiveSuffix.zip"
$manifestFilename = "update-manifest.json"
$semVerPattern = '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$'
if ($Version -notmatch $semVerPattern) {
    throw "Version must be a valid SemVer value (for example, 1.10.26): $Version"
}
if (
    [string]::IsNullOrWhiteSpace($ArchiveSuffix) -or
    $ArchiveSuffix.Contains("/") -or
    $ArchiveSuffix.Contains([string][char]92)
) {
    throw "ArchiveSuffix must be a non-empty filename component: $ArchiveSuffix"
}
# Write the human-facing guide before collecting deployment files so the ZIP
# itself contains the same archive/version information as the handoff folder.
# Build the Unicode filename from code points: Windows PowerShell 5.1 can
# decode a UTF-8 script without BOM using the local code page.
$latestGuideName = [string]::new([char[]](26368, 26032, 37096, 32626, 21253)) + ".txt"
$guidePath = Join-Path $packageRoot.FullName $latestGuideName
$sidecarFilename = "Joker_Baota_Remote_v$Version-$ArchiveSuffix.zip.sha256"
$guideText = @"
Joker Linux portal deployment package

Current release: $Version
Archive: Joker_Baota_Remote_v$Version-$ArchiveSuffix.zip
Checksum: $sidecarFilename

First install:
  Extract the ZIP into a separate directory and run:
  bash install.sh

Upgrade:
  Extract the ZIP into a separate directory, never inside joker-web, then run:
  bash install.sh --non-interactive

The installer keeps the live project at /www/wwwroot/joker-web (or the saved
JOKER_INSTALL_DIR), creates a versioned rollback backup, and automatically
restores the previous release when the health check fails. The web updater uses
the same flow and removes its task-specific ZIP and staging directory. After a
successful update only the newest two release backups and three .env snapshots
are retained; set JOKER_RELEASE_BACKUPS_KEEP=0..20 before a manual update to
change the release-backup retention.

Diagnostics:
  joker-webctl --check
  joker-webctl --show-version
  joker-webctl --apply-nginx

Baota terminal update:
  joker-webctl --update /www1/joker-install/Joker_Baota_Remote_v$Version-all-in-one.zip
  (fallback for hosts without /www1: /www/wwwroot/joker-install/)

Web update logs:
  /var/log/joker-web-updates/<update-id>.log
  /www1/joker-install/last-status.json
  /www1/joker-install/update-history.json

The web administrator upload queue prefers /www1/joker-install when the
separate /www1 volume exists, and falls back to /www/wwwroot/joker-install on
older single-volume hosts. Stale incoming uploads and processing tasks are
cleaned before an upgrade; the live portal and database are preserved.

This package updates the Linux web portal. SCUM commands reach the Linux
portal through the dedicated HTTPS relay URL configured in the Windows plugin;
the Linux portal then calls the BattleMetrics relay using the private
SCUM_RELAY_KEY in web_client/.env. This package does not install a separate
public SCUM service or open port 8766.
Windows AstrBot and the Joker plugin remain on the Windows computer and must
be updated separately. The Linux portal uses the authenticated relay for
SCUM keyword searches and server-ID details only. Keyword and online command
names remain editable per group in the portal.
"@
[System.IO.File]::WriteAllText($guidePath, $guideText, [System.Text.UTF8Encoding]::new($false))
$deployGuides = @(Get-Item -LiteralPath $guidePath)
if ($deployGuides.Count -lt 1) {
    throw "Cannot locate a deployment guide"
}
# ZIP filenames must stay ASCII-only. Windows PowerShell marks Unicode entry
# names in a way that some Linux unzip builds decode with the wrong code page.
# Keep the source guide name unchanged, but publish a stable ASCII name.
$deployGuideNames = @()
for ($guideIndex = 0; $guideIndex -lt $deployGuides.Count; $guideIndex++) {
    if ($guideIndex -eq 0) {
        $deployGuideNames += "DEPLOYMENT.txt"
    }
    else {
        $deployGuideNames += "DEPLOYMENT-$($guideIndex + 1).txt"
    }
}

# The Baota host only runs the remote customer portal. AstrBot and all Windows
# The Linux portal owns the FTP query, but the read-only SQLite lookup is a
# shared module used by both Windows and Linux.  Omitting it produces a
# misleading successful FTP connection followed by ``query_error`` during
# SteamID validation.
$rootFiles = @("feature_catalog.py", "PROJECT_STRUCTURE.md", "runtime_compat.py", "CHANGELOG.md")
$sourceDirectories = @("web_client", "scum")
$nestedSourceDirectories = @("deploy\linux")
$unneededPortalFiles = @(
    "web_client\README.md",
    "web_client\docker-compose.mysql.yml"
)

function Get-ArchiveRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$FullName
    )

    $base = [System.IO.Path]::GetFullPath($BasePath).TrimEnd([char]92, [char]47)
    $full = [System.IO.Path]::GetFullPath($FullName)
    $prefix = $base + [System.IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Cannot make archive-relative path outside the package root: $FullName"
    }
    return $full.Substring($prefix.Length).Replace([char]92, [char]47)
}

function Get-StreamSha256 {
    param([Parameter(Mandatory = $true)][System.IO.Stream]$Stream)

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($algorithm.ComputeHash($Stream)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Write-UpdateManifest {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ReleaseVersion,
        [Parameter(Mandatory = $true)][string]$ArchiveFileName,
        [Parameter(Mandatory = $true)][string]$ManifestFileName
    )

    if (Test-Path -LiteralPath $ManifestPath) {
        throw "Refusing to overwrite an existing deployment manifest: $ManifestPath"
    }

    $rows = [ordered]@{}
    foreach ($file in @(Get-ChildItem -LiteralPath $SourceRoot -File -Recurse -Force | Sort-Object FullName)) {
            $relativePath = Get-ArchiveRelativePath -BasePath $SourceRoot -FullName $file.FullName
            if ($relativePath -eq $ManifestFileName) {
                throw "A payload file conflicts with the reserved manifest name: $ManifestFileName"
            }
            $fileHash = Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256
            $row = [ordered]@{
                sha256 = $fileHash.Hash.ToLowerInvariant()
                size = [Int64]$file.Length
            }
            [void]$rows.Add($relativePath, $row)
    }
    if ($rows.Count -eq 0) {
        throw "Deployment manifest cannot be created from an empty package"
    }

    $manifest = [ordered]@{
        format_version = 1
        release_version = $ReleaseVersion
        archive_filename = $ArchiveFileName
        files = $rows
    }
    $json = ConvertTo-Json -InputObject $manifest -Depth 5
    [System.IO.File]::WriteAllText(
        $ManifestPath,
        $json + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Write-PosixZipArchive {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$DestinationPath
    )

    # Windows PowerShell's Compress-Archive has historically emitted the
    # host's backslash separator into ZIP entry names.  Linux unzip then keeps
    # those backslashes as literal characters and the manifest verifier sees a
    # different tree.  Create each entry explicitly with a POSIX name so the
    # same package is valid on Windows and Linux.
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $fileStream = [System.IO.File]::Open(
        $DestinationPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
    $archive = [System.IO.Compression.ZipArchive]::new(
        $fileStream,
        [System.IO.Compression.ZipArchiveMode]::Create,
        $false
    )
    try {
        foreach ($file in @(Get-ChildItem -LiteralPath $SourceRoot -File -Recurse -Force | Sort-Object FullName)) {
            $relativePath = Get-ArchiveRelativePath -BasePath $SourceRoot -FullName $file.FullName
            $entry = $archive.CreateEntry(
                $relativePath,
                [System.IO.Compression.CompressionLevel]::Optimal
            )
            $sourceStream = $file.OpenRead()
            $entryStream = $entry.Open()
            try {
                $sourceStream.CopyTo($entryStream)
            }
            finally {
                $entryStream.Dispose()
                $sourceStream.Dispose()
            }
        }
    }
    finally {
        $archive.Dispose()
        $fileStream.Dispose()
    }
}

function Test-ArchiveManifest {
    param(
        [Parameter(Mandatory = $true)]$Archive,
        [Parameter(Mandatory = $true)][string]$ManifestFileName,
        [Parameter(Mandatory = $true)][string]$ExpectedArchiveFileName,
        [Parameter(Mandatory = $true)][string]$ExpectedReleaseVersion
    )

    $manifestEntries = @(
        $Archive.Entries | Where-Object {
            $_.FullName.Replace([char]92, [char]47).TrimEnd("/") -eq $ManifestFileName
        }
    )
    if ($manifestEntries.Count -ne 1 -or $manifestEntries[0].FullName.EndsWith("/")) {
        throw "Archive must contain exactly one file named $ManifestFileName"
    }

    $reader = [System.IO.StreamReader]::new(
        $manifestEntries[0].Open(),
        [System.Text.Encoding]::UTF8,
        $true
    )
    try {
        $manifestText = $reader.ReadToEnd()
    }
    finally {
        $reader.Dispose()
    }
    try {
        $manifest = $manifestText | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "Cannot parse $ManifestFileName in archive: $($_.Exception.Message)"
    }

    if (
        $manifest.format_version -ne 1 -or
        $manifest.release_version -ne $ExpectedReleaseVersion -or
        $manifest.archive_filename -ne $ExpectedArchiveFileName
    ) {
        throw "Archive manifest metadata does not match this release"
    }

    if ($null -eq $manifest.PSObject.Properties["files"]) {
        throw "Archive manifest is missing the files map"
    }
    $manifestRows = @($manifest.files.PSObject.Properties)
    if ($manifestRows.Count -eq 0) {
        throw "Archive manifest does not list payload files"
    }
    $manifestByPath = [System.Collections.Generic.Dictionary[string, object]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($property in $manifestRows) {
        $path = [string]$property.Name
        $row = $property.Value
        if (
            $null -eq $row -or
            $null -eq $row.PSObject.Properties["sha256"] -or
            $null -eq $row.PSObject.Properties["size"]
        ) {
            throw "Archive manifest contains an incomplete file record"
        }
        $hash = [string]$row.sha256
        if (
            [string]::IsNullOrWhiteSpace($path) -or
            $path -eq $ManifestFileName -or
            $path.Contains([string][char]92) -or
            $path.StartsWith("/") -or
            $path.Split("/") -contains ".." -or
            $path.Split("/") -contains "." -or
            $hash -notmatch "^[0-9a-fA-F]{64}$"
        ) {
            throw "Archive manifest contains an invalid file record: $path"
        }
        if ($manifestByPath.ContainsKey($path)) {
            throw "Archive manifest contains duplicate file records: $path"
        }
        $manifestByPath.Add($path, $row)
    }

    $archiveByPath = [System.Collections.Generic.Dictionary[string, object]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($entry in $Archive.Entries) {
        $entryName = $entry.FullName.Replace([char]92, [char]47)
        if ($entryName.EndsWith("/")) {
            continue
        }
        if (
            [string]::IsNullOrWhiteSpace($entryName) -or
            $entryName.StartsWith("/") -or
            $entryName.Split("/") -contains ".." -or
            $entryName.Contains([string][char]92)
        ) {
            throw "Archive contains an unsafe file entry: $entryName"
        }
        if ($archiveByPath.ContainsKey($entryName)) {
            throw "Archive contains duplicate file entries: $entryName"
        }
        $archiveByPath.Add($entryName, $entry)
    }
    [void]$archiveByPath.Remove($ManifestFileName)

    if ($archiveByPath.Count -ne $manifestByPath.Count) {
        throw "Archive manifest file count does not match payload"
    }
    foreach ($path in $archiveByPath.Keys) {
        if (-not $manifestByPath.ContainsKey($path)) {
            throw "Archive file is not listed in manifest: $path"
        }
        $entry = $archiveByPath[$path]
        $row = $manifestByPath[$path]
        if ([Int64]$row.size -ne [Int64]$entry.Length) {
            throw "Archive manifest size mismatch: $path"
        }
        $stream = $entry.Open()
        try {
            $actualHash = Get-StreamSha256 -Stream $stream
        }
        finally {
            $stream.Dispose()
        }
        if ($actualHash -ne ([string]$row.sha256).ToLowerInvariant()) {
            throw "Archive manifest SHA-256 mismatch: $path"
        }
    }
}

if (Test-Path -LiteralPath $stagingRoot) {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null

foreach ($relativePath in $rootFiles) {
    Copy-Item -LiteralPath (Join-Path $pluginRoot $relativePath) -Destination $stagingRoot -Force
}

foreach ($directory in $sourceDirectories) {
    $source = Join-Path $pluginRoot $directory
    $destination = Join-Path $stagingRoot $directory
    if (Test-Path -LiteralPath $destination) {
        Remove-Item -LiteralPath $destination -Recurse -Force
    }
    Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
}

foreach ($directory in $nestedSourceDirectories) {
    $source = Join-Path $pluginRoot $directory
    $destination = Join-Path $stagingRoot $directory
    $destinationParent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
    if (Test-Path -LiteralPath $destination) {
        Remove-Item -LiteralPath $destination -Recurse -Force
    }
    Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
}

foreach ($relativePath in $unneededPortalFiles) {
    $target = Join-Path $stagingRoot $relativePath
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Force
    }
}

Get-ChildItem -LiteralPath $stagingRoot -Directory -Recurse -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stagingRoot -File -Recurse | Where-Object {
    $_.Extension -in @(".pyc", ".pyo") -or $_.Name -eq ".env"
} |
    Remove-Item -Force

# Rebuilding the same release is intentional during local development. The
# archive is published only after the new ZIP has passed every structural and
# manifest check; replacing an existing same-version artifact here prevents a
# stale package from surviving a corrected rebuild.

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("joker_baota_" + [guid]::NewGuid().ToString("N"))
$temporaryArchivePath = Join-Path $packageRoot.FullName (
    "joker_baota_archive_" + [guid]::NewGuid().ToString("N") + ".zip"
)
$temporarySource = Join-Path $temporaryRoot "01_plugin_source\astrbot_plugin_joker"
try {
    New-Item -ItemType Directory -Path $temporarySource -Force | Out-Null
    Get-ChildItem -LiteralPath $stagingRoot -Force |
        Copy-Item -Destination $temporarySource -Recurse -Force
    for ($guideIndex = 0; $guideIndex -lt $deployGuides.Count; $guideIndex++) {
        Copy-Item `
            -LiteralPath $deployGuides[$guideIndex].FullName `
            -Destination (Join-Path $temporaryRoot $deployGuideNames[$guideIndex]) `
            -Force
    }
    Copy-Item -LiteralPath (Join-Path $pluginRoot "deploy\linux\quick_install.sh") `
        -Destination (Join-Path $temporaryRoot "install.sh") -Force

    # PowerShell preserves the Windows working tree's CRLF endings. Bash on
    # Linux treats the trailing CR in `set -Eeuo pipefail` and every `case`
    # arm as part of the token, so normalize every shipped shell script before
    # hashing and archiving it. The manifest then verifies the exact LF payload
    # that the Linux installer will execute.
    $shellFiles = @(Get-ChildItem -LiteralPath $temporaryRoot -File -Recurse | Where-Object {
        $_.Extension -eq ".sh"
    })
    $utf8Strict = [System.Text.UTF8Encoding]::new($false, $true)
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    foreach ($shellFile in $shellFiles) {
        $text = [System.IO.File]::ReadAllText($shellFile.FullName, $utf8Strict)
        $normalized = $text.Replace("`r`n", "`n").Replace("`r", "`n")
        [System.IO.File]::WriteAllText($shellFile.FullName, $normalized, $utf8NoBom)
        $bytes = [System.IO.File]::ReadAllBytes($shellFile.FullName)
        for ($index = 0; $index -lt $bytes.Length; $index++) {
            if ($bytes[$index] -eq 13) {
                throw "Shell script still contains CR after normalization: $($shellFile.FullName)"
            }
        }
    }
    Write-UpdateManifest `
        -SourceRoot $temporaryRoot `
        -ManifestPath (Join-Path $temporaryRoot $manifestFilename) `
        -ReleaseVersion $Version `
        -ArchiveFileName ([System.IO.Path]::GetFileName($archivePath)) `
        -ManifestFileName $manifestFilename

    Write-PosixZipArchive `
        -SourceRoot $temporaryRoot `
        -DestinationPath $temporaryArchivePath

    $forbidden = @(
        "02_existing_data",
        "plugin_data",
        "auth.json",
        "bindings.json",
        "plugin_data/qqadmin/message_cache/",
        "plugin_data\qqadmin\message_cache\",
        "browser_data",
        "/.env",
        "\.env",
        "__pycache__",
        ".pyc",
        ".pyo",
        "01_plugin_source/astrbot_plugin_joker/main.py",
        "01_plugin_source/astrbot_plugin_joker/auth/",
        "01_plugin_source/astrbot_plugin_joker/qqadmin/",
        "01_plugin_source/astrbot_plugin_joker/web_client/readme.md",
        "01_plugin_source/astrbot_plugin_joker/web_client/docker-compose.mysql.yml"
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($temporaryArchivePath)
    try {
        $required = @(
            "01_plugin_source/astrbot_plugin_joker/feature_catalog.py",
            "01_plugin_source/astrbot_plugin_joker/runtime_compat.py",
            "01_plugin_source/astrbot_plugin_joker/scum/__init__.py",
            "01_plugin_source/astrbot_plugin_joker/scum/database.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/__main__.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/app.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/scum_provider.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/scum_query_service.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/release_service.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/models.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/reply_asset_service.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/theme_service.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/cookie_service.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/session_middleware.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/self_update.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/update_log.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/integration_service.py",
            "01_plugin_source/astrbot_plugin_joker/web_client/templates/admin_update.html",
            "01_plugin_source/astrbot_plugin_joker/web_client/templates/admin_update_log.html",
            "01_plugin_source/astrbot_plugin_joker/web_client/templates/admin_integrations.html",
            "01_plugin_source/astrbot_plugin_joker/web_client/templates/group_integrations.html",
            "01_plugin_source/astrbot_plugin_joker/web_client/templates/admin_security.html",
            "01_plugin_source/astrbot_plugin_joker/web_client/templates/appearance.html",
            "01_plugin_source/astrbot_plugin_joker/web_client/requirements.txt",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/install_web.sh",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/health_check.sh",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/joker-web-scan-updates.sh",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/joker-web-apply-update.sh",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/joker-web-manual-update.sh",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/joker-web-secure-stage.py",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/joker-web.service.example",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/nginx-web.conf.example",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/render_nginx_config.py",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/verify_update_manifest.py",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/quick_install.sh",
            "01_plugin_source/astrbot_plugin_joker/deploy/linux/setup.html",
            "install.sh",
            "update-manifest.json"
        )
        $entryNames = @($archive.Entries | ForEach-Object {
            $_.FullName.Replace("\", "/").TrimEnd("/")
        })
        $nonAsciiEntries = @($entryNames | Where-Object { $_ -match '[^\x00-\x7F]' })
        if ($nonAsciiEntries.Count -gt 0) {
            throw "Archive contains non-ASCII filenames: $($nonAsciiEntries -join ', ')"
        }
        $missing = @($required | Where-Object { $_ -notin $entryNames })
        if ($missing.Count -gt 0) {
            throw "Required files missing from archive: $($missing -join ', ')"
        }
        $missingGuides = @($deployGuideNames | Where-Object { $_ -notin $entryNames })
        if ($missingGuides.Count -gt 0) {
            throw "Deployment guides missing from archive: $($missingGuides -join ', ')"
        }
        Test-ArchiveManifest `
            -Archive $archive `
            -ManifestFileName $manifestFilename `
            -ExpectedArchiveFileName ([System.IO.Path]::GetFileName($archivePath)) `
            -ExpectedReleaseVersion $Version

        $matches = @(
            $archive.Entries | Where-Object {
                $entryName = $_.FullName.ToLowerInvariant()
                $forbidden | Where-Object {
                    $needle = $_.ToLowerInvariant()
                    $entryName.Contains($needle) -and -not $entryName.EndsWith(".env.example")
                }
            }
        )
        if ($matches.Count -gt 0) {
            throw "Forbidden files found in archive: $($matches.FullName -join ', ')"
        }
    }
    finally {
        $archive.Dispose()
    }

    # Publish only after every archive check succeeds. This prevents a failed
    # build from leaving a same-version, partially written ZIP that an operator
    # might later upload to the server by mistake.
    if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
        [System.IO.File]::Copy($temporaryArchivePath, $archivePath, $true)
        [System.IO.File]::Delete($temporaryArchivePath)
    }
    else {
        Move-Item -LiteralPath $temporaryArchivePath -Destination $archivePath -ErrorAction Stop
    }
}
finally {
    # ``01_plugin_source`` is only a local assembly area. The deployment ZIP
    # already contains it, so retaining a second unpacked copy beside every
    # release wastes space and makes it too easy to upload stale files.
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
    if (
        (Test-Path -LiteralPath $stagingParent -PathType Container) -and
        @((Get-ChildItem -LiteralPath $stagingParent -Force)).Count -eq 0
    ) {
        Remove-Item -LiteralPath $stagingParent -Force
    }
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $temporaryArchivePath) {
        Remove-Item -LiteralPath $temporaryArchivePath -Force
    }
}

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath
$hashText = "SHA256  $($hash.Hash)  $([System.IO.Path]::GetFileName($archivePath))"
$sidecarPath = "$archivePath.sha256"
[System.IO.File]::WriteAllText($sidecarPath, $hashText + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

# Keep the handoff directory unambiguous.  Operators should see one current
# package rather than accidentally uploading an older ZIP after a rebuild.
# This cleanup runs only after the new archive and sidecar have been fully
# written and verified; a failed build therefore leaves prior releases intact.
if (-not $PreservePreviousReleases) {
    Get-ChildItem -LiteralPath $packageRoot.FullName -File -Filter "Joker_Baota_Remote_v*-all-in-one.zip" |
        Where-Object { $_.FullName -ne $archivePath } |
        Remove-Item -Force
    Get-ChildItem -LiteralPath $packageRoot.FullName -File -Filter "Joker_Baota_Remote_v*-all-in-one.zip.sha256" |
        Where-Object { $_.FullName -ne $sidecarPath } |
        Remove-Item -Force
    Get-ChildItem -LiteralPath $packageRoot.FullName -File -Filter "*.txt" |
        Where-Object { $_.FullName -ne $guidePath } |
        Remove-Item -Force
}

$hash

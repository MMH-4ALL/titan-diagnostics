#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ReleaseUrl = 'https://github.com/MMH-4ALL/titan-diagnostics/releases/latest/download',
    [string]$ArchivePath,
    [string]$ChecksumPath,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Titan\cli'),
    [switch]$NoPathUpdate
)
$ErrorActionPreference = 'Stop'
$version = '0.2.1'
$archiveName = "Titan-$version-windows-x64.zip"
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Use install.sh on Linux.' }
if (-not [Environment]::Is64BitOperatingSystem) { throw 'The Windows release requires a 64-bit operating system.' }
$installRoot = [IO.Path]::GetFullPath($InstallDir)
if ($installRoot.TrimEnd('\') -eq [IO.Path]::GetPathRoot($installRoot).TrimEnd('\')) { throw 'Choose a dedicated Titan folder, not a drive root.' }
$ownerFile = Join-Path $installRoot '.titan-install'
if ((Test-Path -LiteralPath $installRoot) -and -not (Test-Path -LiteralPath $ownerFile)) {
    throw "$installRoot already exists and is not managed by Titan. Choose -InstallDir."
}
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('titan-install-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temporary) | Out-Null
try {
    if ($ArchivePath) {
        if (-not $ChecksumPath) { throw '-ArchivePath requires -ChecksumPath.' }
        $archive = (Resolve-Path -LiteralPath $ArchivePath).Path
        $checksums = (Resolve-Path -LiteralPath $ChecksumPath).Path
    } else {
        if ($ChecksumPath) { throw '-ChecksumPath requires -ArchivePath.' }
        if ([Uri]$ReleaseUrl -and ([Uri]$ReleaseUrl).Scheme -ne 'https') { throw 'Release URL must use HTTPS.' }
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $archive = Join-Path $temporary $archiveName
        $checksums = Join-Path $temporary 'SHA256SUMS.txt'
        Invoke-WebRequest -UseBasicParsing -Uri ($ReleaseUrl.TrimEnd('/') + '/SHA256SUMS.txt') -OutFile $checksums
        Invoke-WebRequest -UseBasicParsing -Uri ($ReleaseUrl.TrimEnd('/') + '/' + $archiveName) -OutFile $archive
    }
    $matchesForArchive = @(Get-Content -LiteralPath $checksums | Where-Object { $_ -match ('^[a-fA-F0-9]{64}  ' + [Regex]::Escape($archiveName) + '$') })
    if ($matchesForArchive.Count -ne 1) { throw "Expected one checksum for $archiveName." }
    $expectedHash = $matchesForArchive[0].Substring(0, 64)
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expectedHash) { throw 'Checksum verification failed. Installation stopped.' }
    [IO.Directory]::CreateDirectory($installRoot) | Out-Null
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($ownerFile, 'Titan installer', $encoding)
    $stageRelative = 'versions\' + $version + '-' + [Guid]::NewGuid().ToString('N')
    $stage = Join-Path $installRoot $stageRelative
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        if ($zip.Entries.Count -gt 5000) { throw 'Unexpected archive entry count.' }
        $size = 0L
        foreach ($entry in $zip.Entries) {
            $size += $entry.Length
            if ($size -gt 536870912) { throw 'Unexpected archive size.' }
            $name = $entry.FullName.Replace('\', '/')
            if ($name -eq 'Titan/') { continue }
            if (-not $name.StartsWith('Titan/')) { throw 'Unexpected archive layout.' }
            $relative = $name.Substring(6)
            if ($relative -match '(^|/)\.\.(/|$)|:|^/' -or (($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) { throw 'Unsafe archive entry.' }
            $destination = [IO.Path]::GetFullPath((Join-Path $stage $relative))
            if (-not $destination.StartsWith($stage + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Archive entry escaped its destination.' }
            if ($name.EndsWith('/')) { [IO.Directory]::CreateDirectory($destination) | Out-Null; continue }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination, $false)
        }
    } finally { $zip.Dispose() }
    $exe = Join-Path $stage 'titan.exe'
    if (-not (Test-Path -LiteralPath $exe) -or -not (Test-Path -LiteralPath (Join-Path $stage '_internal'))) { throw 'The release is incomplete.' }
    $reportedVersion = & $exe --version
    if ($LASTEXITCODE -ne 0 -or ($reportedVersion -join '').Trim() -ne "Titan $version") { throw 'The installed executable failed its version check.' }
    $binDir = Join-Path $installRoot 'bin'
    [IO.Directory]::CreateDirectory($binDir) | Out-Null
    $commandPath = Join-Path $binDir 'titan.cmd'
    # A launcher checks its pointer at runtime, so updates do not alter user data.
    $current = Join-Path $installRoot 'current.txt'
    $pointerTemp = Join-Path $installRoot ('current-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($pointerTemp, $stageRelative, $encoding)
    if (Test-Path -LiteralPath $current) { [IO.File]::Replace($pointerTemp, $current, (Join-Path $installRoot 'previous.txt')) } else { [IO.File]::Move($pointerTemp, $current) }
    $launcher = '@echo off' + "`r`n" + 'setlocal DisableDelayedExpansion' + "`r`n" + 'set /p "TITAN_RUNTIME=" < "%~dp0..\current.txt"' + "`r`n" + '"%~dp0..\%TITAN_RUNTIME%\titan.exe" %*' + "`r`n" + 'exit /b %errorlevel%' + "`r`n"
    [IO.File]::WriteAllText($commandPath, $launcher, [Text.Encoding]::Default)
    if (-not $NoPathUpdate) {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $parts = @($userPath -split ';' | Where-Object { $_ })
        if (-not ($parts | Where-Object { $_.TrimEnd('\') -eq $binDir.TrimEnd('\') })) {
            [Environment]::SetEnvironmentVariable('Path', (($parts + $binDir) -join ';'), 'User')
        }
        if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -eq $binDir.TrimEnd('\') })) { $env:Path = $binDir + ';' + $env:Path }
    }
    Write-Host "Installed Titan $version. Run: titan"
    if ($NoPathUpdate) { Write-Host "Launcher: $commandPath" } else { Write-Host 'Reopen other terminal windows to use the updated PATH.' }
} finally {
    $checkedTemporary = [IO.Path]::GetFullPath($temporary)
    $checkedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $checkedTemporary.StartsWith($checkedTempRoot, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($checkedTemporary) -notlike 'titan-install-*') { throw 'Refusing to clean an unexpected temporary path.' }
    Remove-Item -LiteralPath $checkedTemporary -Recurse -Force
}

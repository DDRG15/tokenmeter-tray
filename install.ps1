# Installs TokenMeter as a desktop application.
#
#   .\install.ps1                  copies dist\TokenMeter.exe to LOCALAPPDATA and creates
#                                  shortcuts on the Desktop and in the Start Menu
#   .\install.ps1 -Autostart       also starts TokenMeter when Windows starts
#   .\install.ps1 -Python          shortcuts that run pythonw -m tokenmeter (development:
#                                  always uses the code in this folder)
#   .\install.ps1 -Rollback        restores the previously installed .exe
#   .\install.ps1 -Uninstall       removes the shortcuts and the installed .exe
#
# The .exe is installed by default because it depends neither on this folder nor
# on Python: moving or deleting the folder does not break the shortcut. Build the
# .exe first with
#   python -m PyInstaller tokenmeter.spec --noconfirm

param(
    [switch]$Autostart,
    [switch]$Python,
    [switch]$Rollback,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$projectDir = $PSScriptRoot
$iconPath = Join-Path $projectDir "tokenmeter\assets\tokenmeter.ico"
$sourceExe = Join-Path $projectDir "dist\TokenMeter.exe"
$appFolder = Join-Path $env:LOCALAPPDATA "Programs\TokenMeter"
$targetExe = Join-Path $appFolder "TokenMeter.exe"

$shortcutPaths = @{
    "Desktop"    = Join-Path ([Environment]::GetFolderPath("Desktop")) "TokenMeter.lnk"
    "Start Menu" = Join-Path ([Environment]::GetFolderPath("Programs")) "TokenMeter.lnk"
    "Startup"    = Join-Path ([Environment]::GetFolderPath("Startup")) "TokenMeter.lnk"
}

function Stop-TokenMeter {
    # A running .exe cannot be overwritten. Stop by path: only the installed copy,
    # never a TokenMeter that is running from dist\.
    $procs = @(Get-Process TokenMeter -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $targetExe })
    if ($procs.Count -eq 0) { return }
    # The CLI asks the main thread to exit, which closes SQLite cleanly.
    # An old version may not understand the flag: after the timeout it is forced.
    try { & $targetExe --shutdown } catch { Write-Warning "Could not request a graceful shutdown." }
    # Collectors and the server shut down one after another, and the exe needs
    # time to start the --shutdown command and release its temp folder.
    $procs | Wait-Process -Timeout 30 -ErrorAction SilentlyContinue
    $alive = @($procs | Where-Object { -not $_.HasExited })
    if ($alive.Count -gt 0) {
        Write-Warning "The application did not exit in time; forcing it to close."
        $alive | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    # Stop-Process returns before Windows releases the .exe: copying right away
    # failed with "the file is being used by another process".
    $procs | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
    Write-Host "TokenMeter closed for the update"
}

function Copy-Exe {
    $staged = Join-Path $appFolder "TokenMeter.staged.exe"
    $previous = Join-Path $appFolder "TokenMeter.previous.exe"
    $source = if ($Rollback) { $previous } else { $sourceExe }
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Source executable not found: $source"
    }
    Copy-Item -LiteralPath $source -Destination $staged -Force
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash) {
        Remove-Item -LiteralPath $staged -Force
        throw "The staged copy does not match the original."
    }
    Stop-TokenMeter
    for ($i = 1; $i -le 10; $i++) {
        try {
            if (Test-Path -LiteralPath $targetExe) {
                [System.IO.File]::Replace($staged, $targetExe, $previous, $true)
            } else {
                Move-Item -LiteralPath $staged -Destination $targetExe
            }
            return
        } catch [System.IO.IOException] {
            if ($i -eq 10) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
}

function Remove-Shortcuts {
    foreach ($name in $shortcutPaths.Keys) {
        $path = $shortcutPaths[$name]
        if (Test-Path $path) {
            Remove-Item $path -Force
            Write-Host "Removed from $name"
        }
    }
}

if ($Uninstall) {
    Remove-Shortcuts
    if (Test-Path $appFolder) {
        $resolved = (Resolve-Path -LiteralPath $appFolder).Path
        $expected = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs\TokenMeter"))
        if ($resolved -ne $expected -or
            ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "The install folder does not match or is a link; it will not be deleted."
        }
        Stop-TokenMeter
        Remove-Item -LiteralPath $resolved -Recurse -Force
        Write-Host "Removed the installed copy in $appFolder"
    }
    Write-Host "TokenMeter uninstalled. The data folder is still at $env:APPDATA\TokenMeter"
    return
}

if ($Python) {
    # pythonw avoids the console window; python.exe would leave one open all the time.
    $target = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $target) {
        $target = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
        if (-not $target) { throw "Python was not found on PATH." }
        Write-Warning "pythonw.exe not found: using python.exe, which leaves a console window open."
    }
    $shortcutArgs = "-m tokenmeter"
    $workDir = $projectDir
    if (-not (Test-Path $iconPath)) {
        Write-Host "Generating the icon..."
        & $target (Join-Path $projectDir "make_icon.py")
    }
    $shortcutIcon = $iconPath
} else {
    if (-not $Rollback -and -not (Test-Path $sourceExe)) {
        throw "Missing $sourceExe. Build it with: python -m PyInstaller tokenmeter.spec --noconfirm"
    }
    New-Item -ItemType Directory -Force $appFolder | Out-Null
    Copy-Exe
    Write-Host "Copied to $targetExe"
    $target = $targetExe
    $shortcutArgs = ""
    $workDir = $appFolder
    # The .exe carries its own icon, so the shortcut does not depend on this folder.
    $shortcutIcon = "$targetExe,0"
}

# Shortcuts are overwritten in place. Keep autostart when updating.
$keepAutostart = $Autostart -or (Test-Path -LiteralPath $shortcutPaths["Startup"])

$shell = New-Object -ComObject WScript.Shell

function New-Shortcut($path, $name) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $shortcutArgs
    $lnk.WorkingDirectory = $workDir
    $lnk.Description = "TokenMeter - LLM token meter in the system tray"
    $lnk.IconLocation = $shortcutIcon
    $lnk.Save()
    Write-Host "Created in $name"
}

New-Shortcut $shortcutPaths["Desktop"] "Desktop"
New-Shortcut $shortcutPaths["Start Menu"] "Start Menu"
if ($keepAutostart) {
    New-Shortcut $shortcutPaths["Startup"] "Startup (starts with Windows)"
}

Write-Host ""
Write-Host "Done. Double-click TokenMeter on the Desktop."
Write-Host "The icon appears behind the ^ arrow in the taskbar."
if (-not $Python) { Write-Host "Previous version can be restored with: .\install.ps1 -Rollback" }

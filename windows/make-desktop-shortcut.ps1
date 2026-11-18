# Puts a "Clip Machine" shortcut on the Desktop that opens the app
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desk = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $desk "Clip Machine.lnk"))
$lnk.TargetPath = "$env:WINDIR\System32\wscript.exe"
$lnk.Arguments = "`"$here\Clip Machine.vbs`""
$lnk.WorkingDirectory = (Split-Path -Parent $here)
$lnk.IconLocation = "$(Split-Path -Parent $here)\icon.ico"
$lnk.Description = "Clip Machine"
$lnk.Save()
Write-Host "Shortcut created on your Desktop: Clip Machine"

# Creates "Skill Pack Manager" shortcuts on the Desktop and in the Start menu.
# Run: powershell -ExecutionPolicy Bypass -File install-shortcut.ps1
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { throw "pythonw.exe not found next to $py" }

$shell = New-Object -ComObject WScript.Shell
$targets = @(
  [Environment]::GetFolderPath('Desktop'),
  (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
)
foreach ($dir in $targets) {
  $lnk = $shell.CreateShortcut((Join-Path $dir 'Skill Pack Manager.lnk'))
  $lnk.TargetPath = $pyw
  $lnk.Arguments = '"' + (Join-Path $root 'launch.pyw') + '"'
  $lnk.WorkingDirectory = $root
  $lnk.IconLocation = (Join-Path $root 'assets\icon.ico')
  $lnk.Description = 'Manage which agent skills each project folder gets'
  $lnk.Save()
  Write-Output "created $($lnk.FullName)"
}

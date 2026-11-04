[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [string]$DataDir,
    [string]$PythonPath
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
if ($Install -and $Uninstall) { throw 'Choose Install or Uninstall, not both.' }
if (-not $PythonPath) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $PythonPath = (& $launcher.Source -3 -c 'import sys; print(sys.executable)').Trim()
    } else {
        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($python -and $python.Source -notlike '*WindowsApps*') { $PythonPath = $python.Source }
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Install Python 3 with pythonw.exe, or pass -PythonPath.'
}
$arguments = @((Join-Path $PSScriptRoot 'install_flclash.py'))
if ($Install) { $arguments += '--install' }
if ($Uninstall) { $arguments += '--uninstall' }
if ($DataDir) { $arguments += @('--data-dir', $DataDir) }
& $PythonPath @arguments
if ($LASTEXITCODE -ne 0) { throw 'FlClash Auto Health did not complete. See the error above.' }

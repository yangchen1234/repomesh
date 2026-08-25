$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$Python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path $Python)) { $Python = 'python' }
if (-not (Test-Path '.venv')) { & $Python -m venv .venv }
& '.venv\Scripts\python.exe' -m pip install -e '.[dev]'
$env:Path = "C:\Program Files\nodejs;$env:Path"
Push-Location dashboard
try { & 'C:\Program Files\nodejs\npm.cmd' install } finally { Pop-Location }
Write-Output 'RepoMesh development dependencies are ready.'


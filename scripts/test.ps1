$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
& '.venv\Scripts\python.exe' -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.venv\Scripts\ruff.exe' check src tests evaluation benchmarks scripts/test-remote-client.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.venv\Scripts\mypy.exe' src/repomesh
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$env:Path = "C:\Program Files\nodejs;$env:Path"
Push-Location dashboard
try {
  & 'C:\Program Files\nodejs\npm.cmd' test
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  & 'C:\Program Files\nodejs\npm.cmd' run build
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally { Pop-Location }

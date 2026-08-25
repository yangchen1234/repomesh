$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
& '.venv\Scripts\python.exe' -m pytest
& '.venv\Scripts\ruff.exe' check src tests evaluation benchmarks
& '.venv\Scripts\mypy.exe' src/repomesh
$env:Path = "C:\Program Files\nodejs;$env:Path"
Push-Location dashboard
try {
  & 'C:\Program Files\nodejs\npm.cmd' test
  & 'C:\Program Files\nodejs\npm.cmd' run build
} finally { Pop-Location }


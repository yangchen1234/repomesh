$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
& '.venv\Scripts\python.exe' -m benchmarks.run @args


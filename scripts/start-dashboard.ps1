$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$env:Path = "C:\Program Files\nodejs;$env:Path"
Set-Location (Join-Path $ProjectRoot 'dashboard')
& 'C:\Program Files\nodejs\npm.cmd' run dev


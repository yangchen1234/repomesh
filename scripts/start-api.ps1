$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
# The CLI preserves loopback by default, rejects wildcard binds, and requires
# REPOMESH_API_TOKEN whenever REPOMESH_HOST is an exact private interface IP.
& '.venv\Scripts\repomesh.exe' serve

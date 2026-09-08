$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
docker compose --project-directory $ProjectRoot up -d --wait postgres qdrant


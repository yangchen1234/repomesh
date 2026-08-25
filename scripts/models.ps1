$ErrorActionPreference = 'Stop'
$Ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
if (-not (Test-Path $Ollama)) { $Ollama = 'ollama' }
& $Ollama pull nomic-embed-text
& $Ollama pull qwen2.5-coder:1.5b
& $Ollama list


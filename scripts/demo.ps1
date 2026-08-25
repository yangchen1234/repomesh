$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BaseUrl = if ($env:REPOMESH_BASE_URL) { $env:REPOMESH_BASE_URL.TrimEnd('/') } else { 'http://127.0.0.1:8787' }
$Headers = @{}
if ($env:REPOMESH_API_TOKEN) { $Headers.Authorization = "Bearer $($env:REPOMESH_API_TOKEN)" }
$Health = Invoke-RestMethod "$BaseUrl/v1/health" -Headers $Headers
$Repo = Invoke-RestMethod "$BaseUrl/v1/repositories" -Method Post -Headers $Headers -ContentType 'application/json' -Body (@{ path = $ProjectRoot } | ConvertTo-Json)
$Job = Invoke-RestMethod "$BaseUrl/v1/repositories/$($Repo.id)/index" -Method Post -Headers $Headers -ContentType 'application/json' -Body (@{ mode = 'incremental'; idempotency_key = "demo-$($Repo.commit_sha)"; wait = $true } | ConvertTo-Json)
$Search = Invoke-RestMethod "$BaseUrl/v1/search" -Method Post -Headers $Headers -ContentType 'application/json' -Body (@{ repository_id = $Repo.id; query = 'Where is reciprocal rank fusion implemented?'; mode = 'hybrid' } | ConvertTo-Json)
$Answer = Invoke-RestMethod "$BaseUrl/v1/answer" -Method Post -Headers $Headers -ContentType 'application/json' -Body (@{ repository_id = $Repo.id; query = 'How does RepoMesh fuse lexical and vector results?' } | ConvertTo-Json)
@{ health = $Health; repository = $Repo; job = $Job; search = $Search; answer = $Answer } | ConvertTo-Json -Depth 8


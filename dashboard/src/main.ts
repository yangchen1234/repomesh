import './style.css'

type Health = {
  node_id: string; version: string; uptime_seconds: number; qdrant_status: string;
  ollama_status: string; embedding_model: string; generation_model: string;
  active_jobs: number; queued_jobs: number; degraded_mode: boolean;
}
type Repository = { id: string; name: string; root: string; commit_sha: string; status: string; indexed_files: number; indexed_chunks: number }
type SearchResult = { file_path: string; symbol_name: string | null; start_line: number; end_line: number; content: string; score: number; lexical_score: number | null; vector_score: number | null; rrf_score: number | null; retrieval_method: string }

const app = document.querySelector<HTMLDivElement>('#app')
let repositories: Repository[] = []
let selectedRepository = ''

export function escapeHtml(value: unknown): string {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]!)
}

export function resultMarkup(result: SearchResult): string {
  return `<article class="result"><div class="result-head"><strong>${escapeHtml(result.file_path)}:${result.start_line}-${result.end_line}</strong><span>${escapeHtml(result.symbol_name || 'file chunk')}</span></div><p class="muted">${escapeHtml(result.retrieval_method)} · score ${result.score.toFixed(5)} · lexical ${result.lexical_score?.toFixed(5) ?? '—'} · vector ${result.vector_score?.toFixed(5) ?? '—'} · RRF ${result.rrf_score?.toFixed(5) ?? '—'}</p><pre>${escapeHtml(result.content)}</pre></article>`
}

function token(): string { return localStorage.getItem('repomeshToken') || '' }
function apiBase(): string { return (localStorage.getItem('repomeshBaseUrl') || '').replace(/\/$/, '') }
async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set('Content-Type', 'application/json')
  if (token()) headers.set('Authorization', `Bearer ${token()}`)
  const response = await fetch(`${apiBase()}${path}`, { ...init, headers })
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `${response.status} ${response.statusText}`)
  return response.json() as Promise<T>
}

function renderShell(): void {
  if (!app) return
  app.innerHTML = `<main class="shell"><header><div><h1>RepoMesh</h1><p class="subtitle">Local code intelligence · Windows compute plane</p></div><div id="health-badge" class="badge">Connecting…</div></header><div class="grid"><section class="card health"><h2>Node health</h2><div id="health" class="stats"></div><h2>Connection</h2><form id="connection-form"><input id="base-url" aria-label="API base URL" placeholder="API base URL (blank = same origin)" value="${escapeHtml(apiBase())}"><input id="token" aria-label="API token" type="password" placeholder="Optional API token" value="${escapeHtml(token())}"><button type="submit" class="secondary">Save</button></form></section><section class="card repositories"><h2>Repositories</h2><form id="register-form"><input id="repo-path" aria-label="Repository path" placeholder="Absolute path to an allowed Git repository" required><button type="submit">Register</button></form><div id="repositories"></div></section><section class="card search"><h2>Search & answer</h2><form id="search-form"><select id="repository" aria-label="Repository"></select><input id="query" aria-label="Query" placeholder="Where is retry backoff implemented?" required><select id="mode" aria-label="Retrieval mode"><option value="hybrid">Hybrid</option><option value="lexical">Lexical</option><option value="dense">Dense vector</option><option value="answer">RAG answer</option></select><button type="submit">Run</button></form><div id="latency" class="latency"></div><div id="error"></div><div id="results" class="results"></div></section></div></main>`
  bindEvents()
}

function bindEvents(): void {
  document.querySelector('#connection-form')!.addEventListener('submit', (event) => {
    event.preventDefault()
    localStorage.setItem('repomeshBaseUrl', (document.querySelector<HTMLInputElement>('#base-url')!).value)
    localStorage.setItem('repomeshToken', (document.querySelector<HTMLInputElement>('#token')!).value)
    void refresh()
  })
  document.querySelector('#register-form')!.addEventListener('submit', async (event) => {
    event.preventDefault(); clearError()
    try {
      await api<Repository>('/v1/repositories', { method: 'POST', body: JSON.stringify({ path: document.querySelector<HTMLInputElement>('#repo-path')!.value }) })
      document.querySelector<HTMLInputElement>('#repo-path')!.value = ''; await refreshRepositories()
    } catch (error) { showError(error) }
  })
  document.querySelector('#search-form')!.addEventListener('submit', async (event) => {
    event.preventDefault(); clearError()
    const button = document.querySelector<HTMLButtonElement>('#search-form button')!; button.disabled = true
    const query = document.querySelector<HTMLInputElement>('#query')!.value
    const mode = document.querySelector<HTMLSelectElement>('#mode')!.value
    selectedRepository = document.querySelector<HTMLSelectElement>('#repository')!.value
    try {
      if (mode === 'answer') {
        const data = await api<{ answer: string; citations: { file_path: string; start_line: number; end_line: number }[]; chunks: SearchResult[]; total_latency_ms: number }>('/v1/answer', { method: 'POST', body: JSON.stringify({ repository_id: selectedRepository, query }) })
        document.querySelector('#latency')!.textContent = `${data.total_latency_ms.toFixed(2)} ms total`
        document.querySelector('#results')!.innerHTML = `<article class="result answer"><strong>Answer</strong><p>${escapeHtml(data.answer)}</p><p class="muted">${data.citations.map(c => `[${escapeHtml(c.file_path)}:${c.start_line}-${c.end_line}]`).join(' ')}</p></article>${data.chunks.map(resultMarkup).join('')}`
      } else {
        const data = await api<{ results: SearchResult[]; latency_ms: number }>('/v1/search', { method: 'POST', body: JSON.stringify({ repository_id: selectedRepository, query, mode }) })
        document.querySelector('#latency')!.textContent = `${data.latency_ms.toFixed(2)} ms retrieval`
        document.querySelector('#results')!.innerHTML = data.results.map(resultMarkup).join('') || '<p class="muted">No matching chunks.</p>'
      }
    } catch (error) { showError(error) } finally { button.disabled = false }
  })
}

async function indexRepository(id: string): Promise<void> {
  clearError()
  try {
    const job = await api<{ id: string }>('/v1/repositories/' + encodeURIComponent(id) + '/index', { method: 'POST', body: JSON.stringify({ mode: 'incremental' }) })
    await pollJob(job.id); await refreshRepositories()
  } catch (error) { showError(error) }
}

async function pollJob(id: string): Promise<void> {
  for (;;) {
    const job = await api<{ status: string; progress_done: number; progress_total: number }>('/v1/jobs/' + encodeURIComponent(id))
    const row = document.querySelector(`[data-job="${id}"]`); if (row) row.textContent = `${job.status} ${job.progress_done}/${job.progress_total}`
    if (!['queued', 'running', 'retrying'].includes(job.status)) return
    await new Promise(resolve => setTimeout(resolve, 500))
  }
}

async function refreshHealth(): Promise<void> {
  const health = await api<Health>('/v1/health')
  const badge = document.querySelector('#health-badge')!; badge.textContent = health.degraded_mode ? 'Degraded' : 'Healthy'; badge.className = `badge ${health.degraded_mode ? 'degraded' : 'healthy'}`
  document.querySelector('#health')!.innerHTML = [['Node', health.node_id], ['Version', health.version], ['Qdrant', health.qdrant_status], ['Ollama', health.ollama_status], ['Embedding', health.embedding_model], ['Generation', health.generation_model], ['Active jobs', health.active_jobs], ['Queued jobs', health.queued_jobs]].map(([key, value]) => `<div class="stat"><span>${escapeHtml(key)}</span>${escapeHtml(value)}</div>`).join('')
}

async function refreshRepositories(): Promise<void> {
  repositories = await api<Repository[]>('/v1/repositories'); if (!selectedRepository && repositories[0]) selectedRepository = repositories[0].id
  document.querySelector('#repositories')!.innerHTML = repositories.map(repo => `<div class="repo"><div><strong>${escapeHtml(repo.name)}</strong><div class="muted">${escapeHtml(repo.root)} · ${repo.indexed_files} files · ${repo.indexed_chunks} chunks</div></div><span>${escapeHtml(repo.status)}</span><button data-index="${repo.id}" class="secondary">Index</button><span data-job="${repo.id}"></span></div>`).join('') || '<p class="muted">No repositories registered yet.</p>'
  document.querySelectorAll<HTMLButtonElement>('[data-index]').forEach(button => button.addEventListener('click', () => void indexRepository(button.dataset.index!)))
  document.querySelector('#repository')!.innerHTML = repositories.map(repo => `<option value="${repo.id}" ${repo.id === selectedRepository ? 'selected' : ''}>${escapeHtml(repo.name)}</option>`).join('')
}

function showError(error: unknown): void { document.querySelector('#error')!.innerHTML = `<div class="error">${escapeHtml(error instanceof Error ? error.message : error)}</div>` }
function clearError(): void { document.querySelector('#error')!.innerHTML = '' }
async function refresh(): Promise<void> { clearError(); try { await Promise.all([refreshHealth(), refreshRepositories()]) } catch (error) { showError(error) } }

if (app) {
  renderShell()
  void refresh()
  setInterval(() => void refreshHealth().catch(showError), 5000)
}

import './style.css'
import { escapeHtml, historyMarkup, jobsMarkup, watchMarkup, workersMarkup, type History, type Job, type JobPage, type Watch, type Worker } from './operations'
export { escapeHtml } from './operations'

type Health = {
  node_id: string; version: string; uptime_seconds: number; qdrant_status: string;
  ollama_status: string; embedding_model: string; generation_model: string;
  active_jobs: number; queued_jobs: number; degraded_mode: boolean;
  postgres_status: string; active_workers: number;
}
type Repository = { id: string; name: string; root: string; commit_sha: string; status: string; indexed_files: number; indexed_chunks: number }
type SearchResult = { file_path: string; symbol_name: string | null; start_line: number; end_line: number; content: string; score: number; lexical_score: number | null; vector_score: number | null; rrf_score: number | null; retrieval_method: string }

let app = document.querySelector<HTMLDivElement>('#app')
let repositories: Repository[] = []
let selectedRepository = ''
let watches: Watch[] = []
let watchesLoaded = false
let jobs: Job[] = []
let jobOffset = 0
let detailId = ''
let detailVersion = 0
let refreshVersion = 0
let refreshing = false
let connectionVersion = 0
let connectionAbort = new AbortController()
const pendingActions = new Set<string>()

export function resultMarkup(result: SearchResult): string {
  return `<article class="result"><div class="result-head"><strong>${escapeHtml(result.file_path)}:${result.start_line}-${result.end_line}</strong><span>${escapeHtml(result.symbol_name || 'file chunk')}</span></div><p class="muted">${escapeHtml(result.retrieval_method)} · score ${result.score.toFixed(5)} · lexical ${result.lexical_score?.toFixed(5) ?? '—'} · vector ${result.vector_score?.toFixed(5) ?? '—'} · RRF ${result.rrf_score?.toFixed(5) ?? '—'}</p><pre>${escapeHtml(result.content)}</pre></article>`
}

function token(): string { return localStorage.getItem('repomeshToken') || '' }
function apiBase(): string { return (localStorage.getItem('repomeshBaseUrl') || '').replace(/\/$/, '') }
async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const version = connectionVersion
  const headers = new Headers(init?.headers)
  headers.set('Content-Type', 'application/json')
  if (token()) headers.set('Authorization', `Bearer ${token()}`)
  const response = await fetch(`${apiBase()}${path}`, { ...init, headers, signal: AbortSignal.any([connectionAbort.signal, AbortSignal.timeout(15000)]) })
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `${response.status} ${response.statusText}`)
  const body = await response.json() as T
  if (version !== connectionVersion) throw new DOMException('Connection changed', 'AbortError')
  return body
}

function renderShell(): void {
  if (!app) return
  app.innerHTML = `<main class="shell"><header><div><h1>RepoMesh</h1><p class="subtitle">Local code intelligence · Windows compute plane</p></div><div id="health-badge" class="badge">Connecting…</div></header><div class="grid"><section class="card health"><h2>Node health</h2><div id="health" class="stats"></div><h2>Connection</h2><form id="connection-form"><input id="base-url" aria-label="API base URL" placeholder="API base URL (blank = same origin)" value="${escapeHtml(apiBase())}"><input id="token" aria-label="API token" type="password" placeholder="Optional API token" value="${escapeHtml(token())}"><button type="submit" class="secondary">Save</button></form></section><section class="card repositories"><h2>Repositories</h2><form id="register-form"><input id="repo-path" aria-label="Repository path" placeholder="Absolute path to an allowed Git repository" required><button type="submit">Register</button></form><div id="repositories"></div></section><section class="card search"><h2>Search & answer</h2><form id="search-form"><select id="repository" aria-label="Repository"></select><input id="query" aria-label="Query" placeholder="Where is retry backoff implemented?" required><select id="mode" aria-label="Retrieval mode"><option value="hybrid">Hybrid</option><option value="lexical">Lexical</option><option value="dense">Dense vector</option><option value="answer">RAG answer</option></select><button type="submit">Run</button></form><div id="latency" class="latency"></div><div id="error"></div><div id="results" class="results"></div></section></div></main>`
  document.querySelector('.subtitle')!.textContent = 'Code search · automatic updates · worker operations'
  document.querySelector('.grid')!.before(document.querySelector('#error')!)
  document.querySelector('#error')!.setAttribute('role', 'alert')
  document.querySelector('.search')!.insertAdjacentHTML('beforebegin', `
    <section class="card operations"><div class="section-heading"><h2>Jobs</h2><button class="secondary" id="refresh-operations">Refresh</button></div>
    <p class="muted">Updates every 3 seconds. Pause a repository to stop future automatic submissions; existing jobs can be cancelled below.</p>
    <div id="operations-notice" role="status"></div>
    <div class="filters"><select id="job-repository" aria-label="Filter jobs by repository"><option value="">All repositories</option></select>
    <select id="job-status" aria-label="Filter jobs by status"><option value="active">Active queue</option><option value="">All jobs</option><option value="failed">Failed</option><option value="completed_with_errors">Completed with errors</option><option value="cancelled">Cancelled</option><option value="completed">Completed</option></select></div>
    <div id="jobs" aria-live="polite"></div><div class="pagination"><button id="previous-jobs" class="secondary" disabled>Previous</button><span id="job-page" class="muted"></span><button id="next-jobs" class="secondary" disabled>Next</button></div>
    <div id="job-detail" class="job-detail" hidden></div></section>
    <section class="card operations"><h2>Workers</h2><div id="workers"></div></section>`)
  bindEvents()
}

function bindEvents(): void {
  document.querySelector('#connection-form')!.addEventListener('submit', (event) => {
    event.preventDefault()
    localStorage.setItem('repomeshBaseUrl', (document.querySelector<HTMLInputElement>('#base-url')!).value)
    localStorage.setItem('repomeshToken', (document.querySelector<HTMLInputElement>('#token')!).value)
    connectionVersion++; connectionAbort.abort(); connectionAbort = new AbortController()
    refreshVersion++; detailVersion++; detailId = ''; jobOffset = 0
    repositories = []; watches = []; watchesLoaded = false; jobs = []; selectedRepository = ''
    document.querySelector('#job-detail')!.setAttribute('hidden', '')
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
  document.querySelector('#repository')!.addEventListener('change', event => { selectedRepository = (event.target as HTMLSelectElement).value })
  for (const id of ['job-repository', 'job-status']) document.querySelector('#' + id)!.addEventListener('change', () => { jobOffset = 0; void refreshOperations().catch(showError) })
  document.querySelector('#previous-jobs')!.addEventListener('click', () => { jobOffset = Math.max(0, jobOffset - 25); void refreshOperations().catch(showError) })
  document.querySelector('#next-jobs')!.addEventListener('click', () => { jobOffset += 25; void refreshOperations().catch(showError) })
  document.querySelector('#refresh-operations')!.addEventListener('click', () => void refresh())
  app!.addEventListener('click', event => {
    const button = (event.target as Element).closest<HTMLButtonElement>('button')
    if (!button || button.disabled) return
    if (button.hasAttribute('data-close-detail')) { detailId = ''; detailVersion++; document.querySelector('#job-detail')!.setAttribute('hidden', ''); return }
    if (button.dataset.detail) { detailId = button.dataset.detail; void refreshDetail().catch(showError); return }
    if (button.dataset.index || button.dataset.watch || button.dataset.cancel || button.dataset.retry) void performAction(button)
  })
}

async function indexRepository(id: string): Promise<void> {
  const job = await api<Job>('/v1/repositories/' + encodeURIComponent(id) + '/index', { method: 'POST', body: JSON.stringify({ mode: 'incremental' }) })
  detailId = job.id
}

async function performAction(button: HTMLButtonElement): Promise<void> {
  const version = connectionVersion
  const { index, watch, cancel, retry } = button.dataset
  const key = index || watch || cancel || retry || ''
  if (pendingActions.has(key)) return
  pendingActions.add(key); button.disabled = true; clearError()
  try {
    if (index) await indexRepository(index)
    if (watch) await api('/v1/repositories/' + encodeURIComponent(watch) + '/watch', { method: 'PUT', body: JSON.stringify({ enabled: button.dataset.enabled === 'true' }) })
    if (cancel) await api('/v1/jobs/' + encodeURIComponent(cancel) + '/cancel', { method: 'POST' })
    if (retry) { const job = await api<Job>('/v1/jobs/' + encodeURIComponent(retry) + '/retry', { method: 'POST' }); detailId = job.id }
    if (version === connectionVersion) await refresh()
  } catch (error) { if (version === connectionVersion) showError(error) } finally { pendingActions.delete(key); button.disabled = false }
}

async function refreshHealth(): Promise<void> {
  const health = await api<Health>('/v1/health')
  const badge = document.querySelector('#health-badge')!; badge.textContent = health.degraded_mode ? 'Degraded' : 'Healthy'; badge.className = `badge ${health.degraded_mode ? 'degraded' : 'healthy'}`
  document.querySelector('#health')!.innerHTML = [['Node', health.node_id], ['Version', health.version], ['Coordination', health.postgres_status], ['Workers online', health.active_workers], ['Qdrant', health.qdrant_status], ['Ollama', health.ollama_status], ['Embedding', health.embedding_model], ['Generation', health.generation_model], ['Active jobs', health.active_jobs], ['Queued jobs', health.queued_jobs]].map(([key, value]) => `<div class="stat"><span>${escapeHtml(key)}</span>${escapeHtml(value)}</div>`).join('')
}

async function refreshRepositories(): Promise<void> {
  repositories = await api<Repository[]>('/v1/repositories'); if (!selectedRepository && repositories[0]) selectedRepository = repositories[0].id
  renderRepositories()
  document.querySelector('#repository')!.innerHTML = repositories.map(repo => `<option value="${repo.id}" ${repo.id === selectedRepository ? 'selected' : ''}>${escapeHtml(repo.name)}</option>`).join('')
  const filter = document.querySelector<HTMLSelectElement>('#job-repository')!; const selection = filter.value
  filter.innerHTML = '<option value="">All repositories</option>' + repositories.map(repo => `<option value="${escapeHtml(repo.id)}">${escapeHtml(repo.name)}</option>`).join(''); filter.value = selection
}

function renderRepositories(): void {
  document.querySelector('#repositories')!.innerHTML = repositories.map(repo => `<div class="repo"><div><strong>${escapeHtml(repo.name)}</strong><div class="muted">${escapeHtml(repo.root)} · ${repo.indexed_files} files · ${repo.indexed_chunks} chunks</div></div><span>${escapeHtml(repo.status)}</span><button data-index="${escapeHtml(repo.id)}" class="secondary">Index now</button>${watchesLoaded ? watchMarkup(watches.find(w => w.repository_id === repo.id), repo.id) : '<p class="muted watch-info">Loading automatic update status…</p>'}</div>`).join('') || '<p class="empty">Register a repository to start searching your code.</p>'
}

async function refreshDetail(): Promise<void> {
  if (!detailId) return
  const id = detailId; const version = ++detailVersion
  const panel = document.querySelector<HTMLDivElement>('#job-detail')!; panel.hidden = false
  if (!panel.innerHTML) panel.textContent = 'Loading job details…'
  const [job, history] = await Promise.all([api<Job>('/v1/jobs/' + encodeURIComponent(id)), api<History>('/v1/jobs/' + encodeURIComponent(id) + '/history')])
  if (id !== detailId || version !== detailVersion) return
  panel.innerHTML = historyMarkup(job, history, repositories.find(r => r.id === job.repository_id)?.name || 'Repository')
}

async function refreshOperations(): Promise<void> {
  const version = ++refreshVersion
  const query = new URLSearchParams({ limit: '25', offset: String(jobOffset) })
  const status = document.querySelector<HTMLSelectElement>('#job-status')!.value
  const repository = document.querySelector<HTMLSelectElement>('#job-repository')!.value
  if (status) query.set('status', status)
  if (repository) query.set('repository_id', repository)
  const [page, workers, nextWatches] = await Promise.all([api<JobPage>('/v1/jobs?' + query), api<Worker[]>('/v1/workers'), api<Watch[]>('/v1/watches')])
  if (version !== refreshVersion) return
  if (page.total > 0 && jobOffset >= page.total) { jobOffset = Math.floor((page.total - 1) / 25) * 25; return refreshOperations() }
  jobs = page.jobs; watches = nextWatches; watchesLoaded = true
  const names = new Map(repositories.map(repo => [repo.id, repo.name]))
  document.querySelector('#jobs')!.innerHTML = jobsMarkup(jobs, names, detailId)
  document.querySelector('#workers')!.innerHTML = workersMarkup(workers, new Map(jobs.map(job => [job.id, names.get(job.repository_id) || 'Current job'])))
  document.querySelector('#job-page')!.textContent = page.total ? `${jobOffset + 1}–${jobOffset + page.jobs.length} of ${page.total} jobs` : '0 jobs'
  document.querySelector<HTMLButtonElement>('#previous-jobs')!.disabled = jobOffset === 0
  document.querySelector<HTMLButtonElement>('#next-jobs')!.disabled = jobOffset + 25 >= page.total
  renderRepositories()
  await refreshDetail()
}

function showError(error: unknown): void {
  if (error instanceof DOMException && error.name === 'AbortError') return
  const target = document.querySelector('#error')
  if (target) target.innerHTML = `<div class="error">${escapeHtml(error instanceof Error ? error.message : error)}</div>`
}
function clearError(): void { document.querySelector('#error')!.innerHTML = '' }
async function refresh(): Promise<void> {
  const version = connectionVersion
  try {
    await Promise.all([refreshHealth(), refreshRepositories()])
    await refreshOperations()
    document.querySelector('#operations-notice')!.textContent = ''
  } catch (error) {
    if (version !== connectionVersion) return
    document.querySelector('#operations-notice')!.textContent = 'Connection unavailable. Displayed data may be out of date.'
    const badge = document.querySelector('#health-badge')!; badge.textContent = 'Connection unavailable'; badge.className = 'badge degraded'
    showError(error)
  }
}

export function mountDashboard(): () => void {
  app = document.querySelector<HTMLDivElement>('#app')
  if (!app) return () => {}
  connectionVersion++; connectionAbort.abort(); connectionAbort = new AbortController()
  repositories = []; watches = []; watchesLoaded = false; jobs = []; detailId = ''; selectedRepository = ''; jobOffset = 0
  detailVersion++; refreshVersion++; pendingActions.clear()
  renderShell()
  void refresh()
  const timer = setInterval(() => {
    if (refreshing || document.hidden) return
    refreshing = true; void refresh().finally(() => { refreshing = false })
  }, 3000)
  return () => { clearInterval(timer); connectionVersion++; connectionAbort.abort(); refreshVersion++; detailVersion++ }
}
if (app) mountDashboard()

// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { historyMarkup, jobsMarkup, watchMarkup, workersMarkup, type Job } from './operations'
import { mountDashboard } from './main'

const job: Job = {
  id: 'job-one', repository_id: 'repo-one', status: 'running', trigger: 'watch', mode: 'incremental',
  progress_done: 1, progress_total: 4, attempt: 1, max_attempts: 3, worker_id: 'worker-one',
  available_at: '2026-09-08T12:00:00Z', created_at: '2026-09-08T12:00:00Z', completed_at: null,
  cancel_requested: false, error: null, error_count: 0, parent_job_id: null,
}
const repository = { id: 'repo-one', name: 'Example code', root: '/code', status: 'indexed', indexed_files: 4, indexed_chunks: 8 }
const watch = { repository_id: 'repo-one', enabled: true, state: 'watching', watcher_online: true,
  last_checked_at: '2026-09-08T12:00:00Z', last_indexed_at: null, last_job_id: job.id, error: null }

describe('operations rendering', () => {
  it('shows cancellation only while active and retains progress and retry timing', () => {
    const active = jobsMarkup([{ ...job, status: 'retrying' }], new Map([['repo-one', '<img src=x>']]), '')
    expect(active).toContain('data-cancel=')
    expect(active).not.toContain('data-retry=')
    expect(active).toContain('1/4 files')
    expect(active).toContain('Next attempt:')
    expect(active).toContain('&lt;img src=x&gt;')
    const finished = jobsMarkup([{ ...job, status: 'failed' }], new Map(), '')
    expect(finished).toContain('data-retry=')
    expect(finished).not.toContain('data-cancel=')
  })
  it('shows unavailable watcher and last check without claiming freshness', () => {
    const html = watchMarkup({ ...watch, state: 'watcher_unavailable', watcher_online: false }, 'repo-one')
    expect(html).toContain('Waiting for watcher')
    expect(html).toContain('Last successful index: Not yet')
    expect(html).toContain('Pause automatic updates')
  })
  it('escapes failure text and preserves reclaimed attempts and truncated error count', () => {
    const html = historyMarkup({ ...job, error: '<script>bad()</script>' }, {
      attempts: [{ attempt: 2, worker_id: '<worker>', claimed_at: job.created_at, ended_at: null, outcome: null, reclaimed: true, error: '<img onerror=x>' }],
      file_errors: [{ file_path: '<file>', error: '<script>secret</script>', lease_generation: 2 }], file_error_total: 201,
    }, '<repo>')
    expect(html).toContain('recovered after interruption')
    expect(html).toContain('Showing the latest 200 file errors')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<img ')
  })
  it('shows offline workers and current job navigation', () => {
    expect(workersMarkup([{ worker_id: 'w', hostname: '<host>', pid: 4, status: 'offline', last_seen_at: job.created_at, current_job_id: job.id, completed_jobs: 2, failed_jobs: 1 }], new Map())).toContain('data-detail="job-one"')
    expect(workersMarkup([], new Map())).toContain('No workers have connected')
  })
})

describe('dashboard interactions', () => {
  let stop: () => void
  let requests: { url: string; init?: RequestInit }[]
  let jobState: Job
  beforeEach(() => {
    document.body.innerHTML = '<div id="app"></div>'
    localStorage.clear()
    requests = []; jobState = { ...job }
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      requests.push({ url, init })
      let body: unknown = {}
      if (url === '/v1/health') body = { degraded_mode: false, active_workers: 1, postgres_status: 'healthy', active_jobs: 1, queued_jobs: 0 }
      else if (url === '/v1/repositories') body = [repository]
      else if (url === '/v1/watches') body = [watch]
      else if (url === '/v1/workers') body = []
      else if (url.startsWith('/v1/jobs?')) body = { jobs: [jobState], total: 1, limit: 25, offset: 0 }
      else if (url.endsWith('/history')) body = { attempts: [], file_errors: [], file_error_total: 0 }
      else if (url.endsWith('/cancel')) { jobState = { ...jobState, status: 'cancelled', cancel_requested: true }; body = jobState }
      else if (url.endsWith('/retry')) body = { ...jobState, id: 'successor', status: 'queued' }
      else if (url.startsWith('/v1/jobs/')) body = jobState
      return { ok: true, json: async () => body }
    }))
    stop = mountDashboard()
  })
  afterEach(() => { stop(); vi.unstubAllGlobals(); document.body.innerHTML = '' })

  it('wires cancel, details and a new retry to the authenticated APIs', async () => {
    localStorage.setItem('repomeshToken', 'example-token')
    await vi.waitFor(() => expect(document.querySelector('[data-cancel]')).not.toBeNull())
    ;(document.querySelector('[data-cancel]') as HTMLButtonElement).click()
    await vi.waitFor(() => expect(document.querySelector('[data-retry]')).not.toBeNull())
    expect(requests.find(r => r.url.endsWith('/cancel'))?.init?.method).toBe('POST')
    expect(new Headers(requests.find(r => r.url.endsWith('/cancel'))?.init?.headers).get('Authorization')).toBe('Bearer example-token')
    ;(document.querySelector('[data-retry]') as HTMLButtonElement).click()
    await vi.waitFor(() => expect(requests.some(r => r.url === '/v1/jobs/successor/history')).toBe(true))
    expect(document.querySelector<HTMLDivElement>('#job-detail')!.hidden).toBe(false)
  })
  it('wires pause and repository/status filtering without changing the search selection', async () => {
    await vi.waitFor(() => expect(document.querySelector('[data-watch]')).not.toBeNull())
    ;(document.querySelector('[data-watch]') as HTMLButtonElement).click()
    await vi.waitFor(() => expect(requests.some(r => r.url.endsWith('/watch'))).toBe(true))
    const update = requests.find(r => r.url.endsWith('/watch'))!
    expect(update.init?.method).toBe('PUT')
    expect(JSON.parse(String(update.init?.body))).toEqual({ enabled: false })
    const status = document.querySelector<HTMLSelectElement>('#job-status')!
    status.value = 'failed'; status.dispatchEvent(new Event('change'))
    await vi.waitFor(() => expect(requests.some(r => r.url.includes('status=failed'))).toBe(true))
    expect(document.querySelector<HTMLSelectElement>('#repository')!.value).toBe('repo-one')
  })
  it('marks stale data when the server is unavailable', async () => {
    await vi.waitFor(() => expect(document.querySelector('[data-cancel]')).not.toBeNull())
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('Network unavailable') }))
    ;(document.querySelector('#refresh-operations') as HTMLButtonElement).click()
    await vi.waitFor(() => expect(document.querySelector('#operations-notice')!.textContent).toContain('out of date'))
    expect(document.querySelector('#health-badge')!.textContent).toBe('Connection unavailable')
  })
})

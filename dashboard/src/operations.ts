export type Job = {
  id: string; repository_id: string; status: string; trigger: string; mode: string;
  progress_done: number; progress_total: number; attempt: number; max_attempts: number;
  worker_id: string | null; available_at: string; created_at: string; completed_at: string | null;
  cancel_requested: boolean; error: string | null; error_count: number; parent_job_id: string | null;
}
export type JobPage = { jobs: Job[]; total: number; limit: number; offset: number }
export type Worker = {
  worker_id: string; hostname: string; pid: number; status: string; last_seen_at: string;
  current_job_id: string | null; completed_jobs: number; failed_jobs: number;
}
export type Watch = {
  repository_id: string; enabled: boolean; state: string; watcher_online: boolean;
  last_checked_at: string | null; last_indexed_at: string | null; error: string | null;
  last_job_id: string | null;
}
export type History = {
  attempts: { attempt: number; worker_id: string; claimed_at: string; ended_at: string | null;
    outcome: string | null; reclaimed: boolean; error: string | null }[];
  file_errors: { file_path: string; error: string; lease_generation: number }[];
  file_error_total: number;
}

export function escapeHtml(value: unknown): string {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]!)
}
export function timeLabel(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'Not yet'
}
export function statusLabel(value: string): string { return value.replaceAll('_', ' ') }
export function isActive(job: Job): boolean { return ['queued', 'running', 'retrying'].includes(job.status) }

export function jobsMarkup(jobs: Job[], names: Map<string, string>, selected: string): string {
  return jobs.map(job => {
    const percent = job.progress_total ? Math.min(100, Math.round(job.progress_done / job.progress_total * 100)) : 0
    return `<article class="job-row ${selected === job.id ? 'selected' : ''}">
      <div><strong>${escapeHtml(names.get(job.repository_id) || 'Repository')}</strong>
        <span class="status ${escapeHtml(job.status)}">${escapeHtml(statusLabel(job.status))}${job.cancel_requested && isActive(job) ? ' · cancelling' : ''}</span>
        <p class="muted">${escapeHtml(job.trigger)} · ${escapeHtml(job.mode)} · ${escapeHtml(timeLabel(job.created_at))}</p>
        <progress max="100" value="${percent}" aria-label="Indexing progress"></progress>
        <span class="muted"> ${job.progress_done}/${job.progress_total} files · attempt ${job.attempt}/${job.max_attempts}</span>
        ${job.status === 'retrying' ? `<p class="muted">Next attempt: ${escapeHtml(timeLabel(job.available_at))}</p>` : ''}
        ${job.error ? `<p class="error-text">${escapeHtml(job.error)}</p>` : ''}
        ${job.error_count ? `<p class="error-text">${job.error_count} file errors — open details</p>` : ''}</div>
      <div class="actions"><button class="secondary" data-detail="${escapeHtml(job.id)}" aria-label="Details for ${escapeHtml(names.get(job.repository_id) || 'repository')} job">Details</button>
        ${isActive(job) ? `<button class="secondary" data-cancel="${escapeHtml(job.id)}" ${job.cancel_requested ? 'disabled' : ''}>${job.cancel_requested ? 'Cancelling…' : 'Cancel'}</button>` : `<button data-retry="${escapeHtml(job.id)}">Run again</button>`}
      </div></article>`
  }).join('') || '<p class="empty">No jobs match these filters.</p>'
}

export function workersMarkup(workers: Worker[], jobNames: Map<string, string>): string {
  return workers.map(worker => `<article class="worker-row"><div><strong>${escapeHtml(worker.hostname)} · process ${worker.pid}</strong>
    <span class="status ${escapeHtml(worker.status)}">${escapeHtml(worker.status)}</span>
    <p class="muted">Last seen ${escapeHtml(timeLabel(worker.last_seen_at))} · ${worker.completed_jobs} completed · ${worker.failed_jobs} failed</p></div>
    ${worker.current_job_id ? `<button class="secondary" data-detail="${escapeHtml(worker.current_job_id)}">${escapeHtml(jobNames.get(worker.current_job_id) || 'View current job')}</button>` : '<span class="muted">No current job</span>'}</article>`
  ).join('') || '<p class="empty">No workers have connected. Start a worker to process queued jobs.</p>'
}

export function watchMarkup(watch: Watch | undefined, repositoryId: string): string {
  const enabled = watch?.enabled ?? false
  const labels: Record<string, string> = {
    paused: 'Automatic updates paused', watcher_unavailable: 'Waiting for watcher — check that the watcher is running',
    scan_error: 'Could not read repository', pending_changes: 'Changes detected · waiting to update',
    needs_attention: 'Last automatic update needs attention', watching: 'Watching for changes',
    queued: 'Update queued', running: 'Updating index', retrying: 'Update will retry',
  }
  return `<div class="watch-info"><span>${escapeHtml(watch ? labels[watch.state] || watch.state : 'Automatic updates not configured')}</span>
    <p class="muted">Last checked: ${escapeHtml(timeLabel(watch?.last_checked_at ?? null))}<br>Last successful index: ${escapeHtml(timeLabel(watch?.last_indexed_at ?? null))}</p>
    ${watch?.error ? `<p class="error-text">${escapeHtml(watch.error)}</p>` : ''}
    <button class="secondary" data-watch="${escapeHtml(repositoryId)}" data-enabled="${!enabled}">${enabled ? 'Pause automatic updates' : 'Enable automatic updates'}</button>
    ${watch?.last_job_id ? `<button class="secondary" data-detail="${escapeHtml(watch.last_job_id)}">Latest automatic job</button>` : ''}</div>`
}

export function historyMarkup(job: Job, history: History, repositoryName: string): string {
  return `<div class="section-heading"><h3>${escapeHtml(repositoryName)} · job details</h3><button class="secondary" data-close-detail>Close</button></div>
    <p><span class="status ${escapeHtml(job.status)}">${escapeHtml(statusLabel(job.status))}</span> ${escapeHtml(job.trigger)} · ${escapeHtml(job.mode)}</p>
    <div class="actions">${isActive(job) ? `<button class="secondary" data-cancel="${escapeHtml(job.id)}" ${job.cancel_requested ? 'disabled' : ''}>${job.cancel_requested ? 'Cancelling…' : 'Cancel job'}</button>` : `<button data-retry="${escapeHtml(job.id)}">Run again</button>`}</div>
    ${job.worker_id ? `<p class="muted">Worker: ${escapeHtml(job.worker_id)}</p>` : ''}
    ${job.parent_job_id ? `<button class="secondary" data-detail="${escapeHtml(job.parent_job_id)}">View previous job</button>` : ''}
    ${job.error ? `<p class="error-text">${escapeHtml(job.error)}</p>` : ''}
    <h4>Execution history</h4>${history.attempts.map(attempt => `<div class="attempt"><strong>Attempt ${attempt.attempt} · ${escapeHtml(statusLabel(attempt.outcome || 'running'))}${attempt.reclaimed ? ' · recovered after interruption' : ''}</strong>
      <p class="muted">${escapeHtml(timeLabel(attempt.claimed_at))} → ${attempt.ended_at ? escapeHtml(timeLabel(attempt.ended_at)) : 'In progress'}<br>${escapeHtml(attempt.worker_id)}</p>
      ${attempt.error ? `<p class="error-text">${escapeHtml(attempt.error)}</p>` : ''}</div>`).join('') || '<p class="muted">Waiting for the first worker.</p>'}
    <h4>File errors (${history.file_error_total})</h4>${history.file_errors.map(error => `<div class="attempt"><strong>${escapeHtml(error.file_path)}</strong><p class="error-text">${escapeHtml(error.error)}</p></div>`).join('') || '<p class="muted">No file errors recorded.</p>'}
    ${history.file_error_total > history.file_errors.length ? '<p class="muted">Showing the latest 200 file errors.</p>' : ''}`
}

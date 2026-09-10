# Automatic indexing and worker operations

RepoMesh can now keep registered local repositories indexed without a manual index
request after each edit. The dashboard exposes the queue, worker availability,
execution attempts, file failures, cancellation, and resubmission.

## Run it

`docker compose up --build --scale worker=4` starts the API, workers, PostgreSQL,
Qdrant, and a separate watcher. Open the dashboard at <http://127.0.0.1:8787>.

For a native installation, run `repomesh watch` alongside `repomesh serve` and one
or more `repomesh worker` processes. Windows also has `scripts/start-watcher.ps1`.
Starting only the API does not start background indexing or a watcher.

New registrations enable automatic indexing by default. API clients may pass
`auto_index: false` when registering; native users can register with
`repomesh register PATH --no-auto-index`. `REPOMESH_WATCH_DEFAULT_ENABLED=false`
changes the default for newly discovered registrations. Existing per-repository
preferences persist in PostgreSQL and are not reset by a restart or registration.
The watcher initializes preferences for repositories registered before this feature.

## What the watcher does

The watcher polls registered, enabled local Git repositories every five seconds.
It uses the indexer's file discovery, Git ignore rules, repository allowlist, secret
exclusions, binary exclusions, and file size limit. It hashes eligible file content
and the current commit, so timestamp-preserving edits and commit changes are detected.
File additions, modifications, deletions, and branch changes enqueue incremental jobs.
This is local change detection; it does not fetch or pull remote Git branches.

Consecutive observations are debounced for two seconds. A pending change has a
maximum debounce wait of thirty seconds, checked on the next scan. Active jobs
still serialize execution: changes observed while a job is queued, running, or
retrying are coalesced into one follow-up job after it finishes. Long scans and busy
workers add latency; these intervals are not wall-clock completion guarantees.

Observations, debounce timestamps, the submitted fingerprint, and the new job are
stored durably. Job submission and the fingerprint advance commit in one PostgreSQL
transaction. A session advisory lock elects one scanner per PostgreSQL schema;
additional watcher processes remain standbys. The lock spans scanning, but no
database transaction spans filesystem discovery or hashing. A watcher process crash
releases its session lock; restart resumes from the persisted state.

The watcher only submits jobs. Workers retain their existing leases, ownership
generation checks, repository exclusion, retries, and cancellation. A failed,
cancelled, or partially failed job is not recreated forever for unchanged content.
The dashboard shows that it needs attention; use **Run again**, or pause and re-enable
automatic updates after fixing the cause. A new content change can trigger another job.
Pausing stops future automatic submissions; already queued/running work is cancelled
separately from the jobs panel.

After successful automatic work, the watcher also reconciles source hashes against
the persisted index manifest. This repairs an edit that a worker read and that was
reverted before the next scan, even when the observed source fingerprint is unchanged.

## Dashboard

- Repositories show automatic update state, last check, and the last successfully
  completed index time, with **Index now**, **Pause/Enable automatic updates**, and a
  link to the latest automatic job.
- Jobs default to the active queue, with repository/status filters and pages of 25.
  Each row shows progress, trigger, attempt budget, retry time, and failure information.
- Details retain execution attempts, worker identities, crash recovery markers,
  whole-job errors, and the latest 200 file errors with a total count.
- **Cancel** signals a running job or cancels a queued/retrying job. **Run again**
  creates an incremental successor with a new attempt budget and a link to the
  original job. Repeating the same request returns the same successor; to run it
  again later, select the successor. Original history remains unchanged.
- Workers show idle/busy/offline/stopped state, last heartbeat, completion/failure
  counts, and their current job. Existing search, answers, and citations remain available.
- The page refreshes every three seconds while visible. Connection failures mark
  displayed data as stale. Automatic update status is not treated as known before
  it loads, and a missing/stale watcher is shown explicitly.

## API additions

All new routes use the existing API token authentication.

| Route | Purpose |
|---|---|
| `GET /v1/jobs` | Paginated history; `repository_id`, `status`, `limit` (1–100), `offset` |
| `GET /v1/jobs/{id}/history` | Attempt history, file errors, total file error count |
| `POST /v1/jobs/{id}/retry` | Idempotent successor for a terminal job; active jobs return 409 |
| `GET /v1/watches` | Automatic update preferences and observed state |
| `PUT /v1/repositories/{id}/watch` | Set `enabled` to true or false |

`status=active` selects queued/running/retrying jobs. Omitting it selects all statuses.
Jobs additionally expose `trigger` (`manual`, `watch`, `retry`) and `parent_job_id`.
The additive PostgreSQL migration preserves existing jobs. Whole-job error text is
retained for new execution attempts; historical attempts cannot recover errors that
older versions did not retain.

## Configuration and limits

| Setting | Default |
|---|---|
| `REPOMESH_WATCH_POLL_SECONDS` | 5 |
| `REPOMESH_WATCH_DEBOUNCE_SECONDS` | 2 |
| `REPOMESH_WATCH_MAX_WAIT_SECONDS` | 30 |
| `REPOMESH_WATCH_DEFAULT_ENABLED` | true |

Polling hashes eligible content on each scan, trading disk reads for dependable
change detection across native and bind-mounted repositories. Large repositories
may need a longer poll interval. A stale watcher threshold is the larger of 30 seconds
or three poll intervals; a particularly long scan can display an unavailable watcher.
Edits during a scan or indexing are reconciled on later scans; the working directory
is not an immutable snapshot. The last successful index timestamp does not promise
that every edit made during that job is included. The shared local storage deployment
boundary and cross-store fencing limitations in the distributed design still apply.

## Verification

The new backend tests use real PostgreSQL and cover durable debounce, continuous-edit
max wait, concurrent submission, filters, authentication, retry idempotency, failure
visibility, pause/resume, scanner election, and separate watcher/worker processes
processing real file changes. Dashboard tests cover escaping, retry/cancel controls,
watcher unavailability, authenticated actions, filtering, and stale connection state.
The Compose smoke additionally restarts the watcher, edits files through the host
bind mount, waits for automatic indexing into real Qdrant, and checks retrieval,
attempt history, pagination, and pausing.

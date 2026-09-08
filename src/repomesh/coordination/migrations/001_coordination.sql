CREATE TABLE repository_execution (
    repository_id text PRIMARY KEY,
    active_job_id text
);

CREATE TABLE jobs (
    id text PRIMARY KEY,
    repository_id text NOT NULL REFERENCES repository_execution(repository_id),
    kind text NOT NULL DEFAULT 'index' CHECK (kind = 'index'),
    mode text NOT NULL CHECK (mode IN ('full','incremental')),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued','running','retrying','completed','completed_with_errors','failed','cancelled')),
    priority integer NOT NULL DEFAULT 0,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    idempotency_key text UNIQUE,
    worker_id text,
    lease_generation bigint NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    completed_at timestamptz,
    heartbeat_at timestamptz,
    lease_expires_at timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    progress_done integer NOT NULL DEFAULT 0,
    progress_total integer NOT NULL DEFAULT 0,
    indexed_files integer NOT NULL DEFAULT 0,
    indexed_chunks integer NOT NULL DEFAULT 0,
    deleted_files integer NOT NULL DEFAULT 0,
    skipped_files integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,
    error text,
    CHECK ((status = 'running') = (worker_id IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK (status = 'running' OR (worker_id IS NULL AND lease_expires_at IS NULL))
);
CREATE INDEX jobs_runnable ON jobs(priority DESC, available_at, created_at)
    WHERE status IN ('queued','retrying');
CREATE INDEX jobs_leases ON jobs(lease_expires_at) WHERE status = 'running';
CREATE INDEX jobs_repository ON jobs(repository_id, created_at DESC);
CREATE UNIQUE INDEX one_running_job_per_repository ON jobs(repository_id)
    WHERE status = 'running';

CREATE TABLE workers (
    worker_id text PRIMARY KEY,
    session_id text NOT NULL,
    hostname text NOT NULL,
    pid integer NOT NULL,
    version text NOT NULL,
    status text NOT NULL CHECK (status IN ('idle','busy','stopped')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    current_job_id text,
    completed_jobs bigint NOT NULL DEFAULT 0,
    failed_jobs bigint NOT NULL DEFAULT 0
);
CREATE INDEX workers_seen ON workers(last_seen_at);

CREATE TABLE job_attempts (
    job_id text NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    lease_generation bigint NOT NULL,
    worker_id text NOT NULL,
    attempt integer NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_expires_at timestamptz NOT NULL,
    ended_at timestamptz,
    outcome text,
    reclaimed boolean NOT NULL DEFAULT false,
    PRIMARY KEY (job_id, lease_generation)
);
CREATE TABLE job_file_errors (
    job_id text NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    lease_generation bigint NOT NULL,
    file_path text NOT NULL,
    error text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX file_errors_job ON job_file_errors(job_id);

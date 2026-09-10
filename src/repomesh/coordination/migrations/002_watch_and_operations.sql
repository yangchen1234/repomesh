ALTER TABLE jobs ADD COLUMN trigger text NOT NULL DEFAULT 'manual'
    CHECK (trigger IN ('manual', 'watch', 'retry'));
ALTER TABLE jobs ADD COLUMN parent_job_id text UNIQUE REFERENCES jobs(id);
ALTER TABLE job_attempts ADD COLUMN error text;
CREATE INDEX jobs_recent ON jobs(created_at DESC, id DESC);

CREATE TABLE repository_watches (
    repository_id text PRIMARY KEY REFERENCES repository_execution(repository_id),
    enabled boolean NOT NULL DEFAULT true,
    observed_fingerprint text,
    submitted_fingerprint text,
    stable_since timestamptz,
    pending_since timestamptz,
    last_checked_at timestamptz,
    last_job_id text REFERENCES jobs(id),
    error text
);

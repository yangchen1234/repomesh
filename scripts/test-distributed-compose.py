"""End-to-end smoke against actual Compose services; standard library only."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8787"


def request(path: str, body: dict[str, Any] | None = None, method: str | None = None) -> Any:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def wait(predicate: Any, timeout: float = 120) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise TimeoutError("Compose smoke condition timed out")


def terminal(job_id: str) -> dict[str, Any] | None:
    job = request(f"/v1/jobs/{job_id}")
    return (
        job
        if job["status"] in {"completed", "completed_with_errors", "failed", "cancelled"}
        else None
    )


def main() -> None:
    evidence: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "embedding": "deterministic-hash",
        "vectors": "qdrant",
        "workers": 4,
    }
    output = ROOT / "benchmarks" / "compose_results.json"
    try:
        wait(lambda: len([w for w in request("/v1/workers") if w["status"] == "idle"]) == 4)
        ids = []
        for index in range(8):
            path = ROOT / "work" / "compose-corpus" / f"repo-{index}"
            path.mkdir(parents=True, exist_ok=True)
            for copy in range(4):
                shutil.copytree(
                    ROOT / "sample_repository", path / f"fixture-{copy}", dirs_exist_ok=True
                )
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            repo = request(
                "/v1/repositories",
                {"path": f"/repositories/repomesh/work/compose-corpus/repo-{index}", "auto_index": False},
            )
            ids.append(repo["id"])
        started = time.perf_counter()
        jobs = [request(f"/v1/repositories/{repo}/index", {"mode": "full"}) for repo in ids]
        results = [wait(lambda job=job: terminal(job["id"])) for job in jobs]
        assert all(
            job["status"] == "completed" and job["indexed_files"] == 60 for job in results
        ), results
        workers = request("/v1/workers")
        assert len(workers) == 4 and all(w["completed_jobs"] > 0 for w in workers), workers
        for mode in ("lexical", "dense", "hybrid"):
            assert request(
                "/v1/search", {"repository_id": ids[0], "query": "inventory", "mode": mode}
            )["results"]
        evidence.update(
            indexing_seconds=time.perf_counter() - started,
            completed_jobs=len(results),
            worker_distribution=workers,
            retrieval_modes=["lexical", "dense", "hybrid"],
        )

        # More real files ensure the kill lands during indexing, after persisted progress.
        path = ROOT / "work" / "compose-corpus" / "repo-0"
        for copy in range(4, 20):
            shutil.copytree(
                ROOT / "sample_repository", path / f"fixture-{copy}", dirs_exist_ok=True
            )
        job = request(f"/v1/repositories/{ids[0]}/index", {"mode": "full"})

        def started_job() -> Any:
            state = request(f"/v1/jobs/{job['id']}")
            return state if state["status"] == "running" and state["indexed_files"] > 0 else None

        owned = wait(started_job)
        victim = next(w for w in request("/v1/workers") if w["worker_id"] == owned["worker_id"])
        containers = subprocess.check_output(
            ["docker", "compose", "ps", "-q", "worker"], text=True
        ).split()
        container = next(c for c in containers if c.startswith(victim["hostname"]))
        killed_at = time.perf_counter()
        subprocess.run(["docker", "kill", container], check=True, capture_output=True)
        recovered = wait(lambda: terminal(job["id"]))
        assert (
            recovered["status"] == "completed"
            and recovered["lease_generation"] > owned["lease_generation"]
        ), recovered
        assert recovered["skipped_files"] >= owned["indexed_files"], recovered
        assert request(
            "/v1/search", {"repository_id": ids[0], "query": "inventory", "mode": "dense"}
        )["results"]
        evidence.update(
            status="passed",
            crash={
                "crashed_workers": 1,
                "successful_recoveries": 1,
                "recovery_seconds": time.perf_counter() - killed_at,
                "before": owned,
                "after": recovered,
            },
        )
        # The watcher sees host edits through the same read-only bind mount as workers.
        auto_root = ROOT / "work" / "compose-corpus" / "auto-updates"
        auto_root.mkdir(parents=True, exist_ok=True)
        (auto_root / "app.py").write_text("def automatic_first(): return 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(auto_root)], check=True)
        auto_repo = request("/v1/repositories", {
            "path": "/repositories/repomesh/work/compose-corpus/auto-updates", "auto_index": True,
        })

        def auto_job(after: str | None = None) -> Any:
            state = next(w for w in request("/v1/watches") if w["repository_id"] == auto_repo["id"])
            job_id = state["last_job_id"]
            return terminal(job_id) if job_id and job_id != after else None

        initial_auto = wait(auto_job)
        assert initial_auto["status"] == "completed", initial_auto
        subprocess.run(["docker", "compose", "restart", "watcher"], check=True, capture_output=True)
        (auto_root / "app.py").unlink()
        (auto_root / "replacement.py").write_text("def automatic_replacement(): return 2\n", encoding="utf-8")
        updated_auto = wait(lambda: auto_job(initial_auto["id"]))
        assert updated_auto["status"] == "completed" and updated_auto["deleted_files"] == 1, updated_auto
        assert request("/v1/search", {
            "repository_id": auto_repo["id"], "query": "automatic_replacement", "mode": "hybrid",
        })["results"][0]["file_path"] == "replacement.py"
        history = request(f"/v1/jobs/{updated_auto['id']}/history")
        assert history["attempts"] and history["file_error_total"] == 0, history
        paused = request(f"/v1/repositories/{auto_repo['id']}/watch", {"enabled": False}, "PUT")
        assert paused["state"] == "paused", paused
        page = request(f"/v1/jobs?repository_id={auto_repo['id']}&status=completed&limit=1")
        assert page["total"] == 2 and len(page["jobs"]) == 1, page
        evidence["automatic_updates"] = {
            "initial": initial_auto, "after_watcher_restart_and_file_changes": updated_auto,
            "history": history, "paused": paused["state"], "completed_jobs": page["total"],
        }
        with urllib.request.urlopen(BASE + "/metrics", timeout=10) as response:
            evidence["metrics"] = response.read().decode()
    except Exception as exc:
        evidence.update(status="failed", error=str(exc))
        raise
    finally:
        output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {k: v for k, v in evidence.items() if k not in {"metrics", "worker_distribution"}},
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

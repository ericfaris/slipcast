"""In-memory tracker for background poll/download jobs.

The UI polls /api/state to show live progress (spinners) and surface a toast
when a job finishes. Jobs live only for the process lifetime — that's fine,
they describe transient work, not durable state.
"""
import itertools
import threading
import time

_lock = threading.Lock()
_jobs: dict[int, dict] = {}
_counter = itertools.count(1)

# Keep the most recent finished jobs so the client has time to render a toast
# for each, then drop them to bound memory.
_MAX_FINISHED = 40


def start(kind: str, target: str) -> int:
    """Register a running job and return its id. kind: 'poll' | 'download'."""
    with _lock:
        jid = next(_counter)
        _jobs[jid] = {
            "id": jid,
            "kind": kind,
            "target": target,
            "status": "running",
            "message": "",
            "started": time.time(),
            "finished": None,
        }
        return jid


def finish(jid: int, status: str, message: str = "") -> None:
    """Mark a job done. status: 'success' | 'error'."""
    with _lock:
        job = _jobs.get(jid)
        if job:
            job.update(status=status, message=message, finished=time.time())
        _prune_locked()


def _prune_locked() -> None:
    finished = [j for j in _jobs.values() if j["finished"] is not None]
    if len(finished) > _MAX_FINISHED:
        finished.sort(key=lambda j: j["finished"])
        for job in finished[: len(finished) - _MAX_FINISHED]:
            _jobs.pop(job["id"], None)


def snapshot() -> list[dict]:
    """Return all tracked jobs, oldest first."""
    with _lock:
        return [dict(j) for j in sorted(_jobs.values(), key=lambda j: j["id"])]

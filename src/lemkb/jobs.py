"""Background jobs for the web interface: one long task at a time (import, fetch, parse,
backup), with its log lines captured so the UI can show live progress."""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from typing import Any, Callable

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_current: str | None = None


class _JobLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        job = _jobs.get(_current) if _current else None
        if job is not None and record.name.startswith("lemkb"):
            job["log"].append(f"{time.strftime('%H:%M:%S')}  {record.getMessage()}")
            del job["log"][:-400]  # keep the tail


_handler = _JobLogHandler(level=logging.INFO)
logging.getLogger("lemkb").addHandler(_handler)
logging.getLogger("lemkb").setLevel(logging.INFO)


def start(kind: str, steps: list[tuple[str, Callable[[], Any]]]) -> dict:
    """Run steps in a background thread. Refuses if another job is running."""
    global _current
    with _lock:
        if _current and _jobs[_current]["status"] == "running":
            raise RuntimeError(f"'{_jobs[_current]['kind']}' is still running")
        job_id = uuid.uuid4().hex[:10]
        _jobs[job_id] = {"id": job_id, "kind": kind, "status": "running", "step": None,
                         "steps": [s for s, _ in steps], "results": {}, "log": [], "error": None,
                         "started": time.time(), "finished": None}
        _current = job_id

    def work():
        job = _jobs[job_id]
        try:
            for name, fn in steps:
                job["step"] = name
                logging.getLogger("lemkb.jobs").info("▶ %s", name)
                job["results"][name] = fn()
            job["status"] = "done"
        except Exception as e:
            job["status"], job["error"] = "failed", f"{e}"
            job["log"].append(traceback.format_exc(limit=3))
        finally:
            job["finished"] = time.time()

    threading.Thread(target=work, name=f"job-{job_id}", daemon=True).start()
    return _jobs[job_id]


def get(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def current() -> dict | None:
    return _jobs.get(_current) if _current else None

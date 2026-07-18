"""Job state persistence (JSON per job) and Redis progress publishing.

Job state lives at:  <PROCESSED_DIR>/<job_id>/job.json
Progress events are published on Redis channel:  progress:<job_id>
so the FastAPI WebSocket relay can stream them to the browser.
"""
import json
import os
import time
import uuid
from pathlib import Path

import redis

from .config import PROCESSED_DIR, REDIS_URL

_redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def new_job_id() -> str:
    return uuid.uuid4().hex


def job_dir(job_id: str) -> Path:
    """Return (and create) the working directory for a job.

    job_id is validated as a UUID hex string so it can never traverse paths.
    """
    if not (len(job_id) == 32 and all(c in "0123456789abcdef" for c in job_id)):
        raise ValueError("Invalid job id")
    d = PROCESSED_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_job(job_id: str) -> dict:
    path = job_dir(job_id) / "job.json"
    if not path.exists():
        raise FileNotFoundError(f"Unknown job: {job_id}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_job(job_id: str, data: dict) -> None:
    """Atomically persist job state."""
    path = job_dir(job_id) / "job.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def update_job(job_id: str, **fields) -> dict:
    data = load_job(job_id)
    data.update(fields)
    save_job(job_id, data)
    return data


def publish_progress(job_id: str, stage: str, progress: float,
                     status: str = "running", message: str = "") -> None:
    """Push a progress event to Redis pub/sub and mirror it into job.json.

    status: "running" | "done" | "error"
    """
    event = {
        "job_id": job_id,
        "stage": stage,
        "progress": round(max(0.0, min(100.0, progress)), 1),
        "status": status,
        "message": message,
        "ts": time.time(),
    }
    try:
        update_job(job_id, last_event=event)
    except FileNotFoundError:
        pass
    _redis.publish(f"progress:{job_id}", json.dumps(event))

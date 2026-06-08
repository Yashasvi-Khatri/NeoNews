from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import threading
import uuid


class IngestionStatusStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {
            "run_id": None,
            "status": "idle",
            "started_at": None,
            "finished_at": None,
            "error": None,
            "stats": None,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, run_id: str | None = None) -> str | None:
        with self._lock:
            if self._state["status"] == "running":
                return None
            run_id = run_id or uuid.uuid4().hex
            self._state = {
                "run_id": run_id,
                "status": "running",
                "started_at": _now(),
                "finished_at": None,
                "error": None,
                "stats": None,
            }
            return run_id

    def complete(self, stats) -> None:
        with self._lock:
            self._state["status"] = "completed"
            self._state["finished_at"] = _now()
            self._state["stats"] = asdict(stats)
            self._state["error"] = None

    def fail(self, exc: Exception) -> None:
        with self._lock:
            self._state["status"] = "failed"
            self._state["finished_at"] = _now()
            self._state["error"] = str(exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


ingestion_status_store = IngestionStatusStore()

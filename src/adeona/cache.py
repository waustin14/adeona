"""File-backed JSON cache with a TTL (default one hour)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_TTL_SECONDS = 3600


class FileCache:
    def __init__(self, directory: str | Path, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.dir = Path(directory)
        self.ttl = ttl_seconds
        self.clock = clock

    @staticmethod
    def key(namespace: str, params: dict[str, Any]) -> str:
        blob = json.dumps({"ns": namespace, "params": params}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> Optional[Any]:
        p = self._path(key)
        try:
            entry = json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if self.clock() - entry["stored_at"] > self.ttl:
            p.unlink(missing_ok=True)
            return None
        return entry["value"]

    def set(self, key: str, value: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps({"stored_at": self.clock(), "value": value}))
        tmp.replace(self._path(key))

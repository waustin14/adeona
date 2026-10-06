"""Append-only JSONL log of every search attempt (live, cached, or refused)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECRET_KEYS = {"api_key", "apikey", "key", "token", "authorization", "access_token"}


def redact(params: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if k.lower() in SECRET_KEYS else v) for k, v in params.items()}


class CallLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def write(self, **record: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
        if isinstance(record.get("params"), dict):
            record["params"] = redact(record["params"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

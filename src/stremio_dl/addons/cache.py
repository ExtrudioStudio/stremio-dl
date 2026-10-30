"""Tiny on-disk JSON cache keyed by URL, with a per-read TTL."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class JsonCache:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.root / digest[:2] / f"{digest}.json"

    def get(self, key: str, ttl: float) -> Any | None:
        path = self._path(key)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - entry.get("t", 0) > ttl:
            return None
        return entry.get("data")

    def set(self, key: str, data: Any) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"t": time.time(), "url": key, "data": data}), "utf-8")
            tmp.replace(path)
        except OSError:
            pass  # a cache that can't be written is just a slower cache

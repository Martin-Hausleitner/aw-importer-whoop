from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DATA_TYPES


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class ImportState:
    last_successful_sync_per_type: dict[str, str] = field(default_factory=dict)
    record_hashes: dict[str, str] = field(default_factory=dict)
    aw_event_ids: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ImportState":
        if not path.exists():
            return cls(last_successful_sync_per_type={k: "" for k in DATA_TYPES})
        with path.open("r", encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
        return cls(
            last_successful_sync_per_type=raw.get("last_successful_sync_per_type", {}),
            record_hashes=raw.get("record_hashes", {}),
            aw_event_ids={k: int(v) for k, v in raw.get("aw_event_ids", {}).items()},
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({
                "last_successful_sync_per_type": self.last_successful_sync_per_type,
                "record_hashes": self.record_hashes,
                "aw_event_ids": self.aw_event_ids,
            }, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        os.chmod(path, 0o600)

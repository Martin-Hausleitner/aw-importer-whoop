from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx

from .config import WHOOP_BASE_URL, WHOOP_TOKEN_URL

ENDPOINTS = {
    "sleep": "/v2/activity/sleep",
    "workout": "/v2/activity/workout",
    "cycle": "/v2/cycle",
    "recovery": "/v2/recovery",
}


class WhoopClient:
    def __init__(self, client_id: str, client_secret: str, token_path: Path, timeout: float = 30.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        self.timeout = timeout
        self.tokens = self._load_tokens()

    def _load_tokens(self) -> dict[str, Any]:
        if self.token_path.exists():
            return json.loads(self.token_path.read_text(encoding="utf-8"))
        return {}

    def _save_tokens(self) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tokens, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.token_path)
        os.chmod(self.token_path, 0o600)

    def refresh_if_needed(self) -> None:
        expires_at = float(self.tokens.get("expires_at", 0))
        if self.tokens.get("access_token") and expires_at - time.time() > 120:
            return
        refresh_token = self.tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("No WHOOP tokens found. Run OAuth login first.")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(WHOOP_TOKEN_URL, data=data)
            r.raise_for_status()
            new_tokens = r.json()
        # Refresh tokens rotate; preserve new token every time.
        self.tokens.update(new_tokens)
        self.tokens["expires_at"] = time.time() + int(new_tokens.get("expires_in", 3600))
        self._save_tokens()

    def paged(self, data_type: str, start: datetime, end: datetime) -> Iterator[dict[str, Any]]:
        endpoint = ENDPOINTS[data_type]
        next_token: str | None = None
        while True:
            self.refresh_if_needed()
            params: dict[str, str] = {"start": start.isoformat(), "end": end.isoformat()}
            if next_token:
                params["nextToken"] = next_token
            headers = {"Authorization": f"Bearer {self.tokens['access_token']}"}
            with httpx.Client(timeout=self.timeout) as client:
                for attempt in range(4):
                    r = client.get(WHOOP_BASE_URL + endpoint, params=params, headers=headers)
                    if r.status_code == 429:
                        wait = int(r.headers.get("Retry-After", "1"))
                        time.sleep(wait)
                        continue
                    if 500 <= r.status_code < 600 and attempt < 3:
                        time.sleep(2**attempt)
                        continue
                    r.raise_for_status()
                    break
            payload = r.json()
            records = payload.get("records") or []
            yield from records
            next_token = payload.get("nextToken")
            if not next_token:
                return


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

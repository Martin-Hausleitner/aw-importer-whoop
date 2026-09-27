from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx

from aw_importer_whoop import whoop


def test_paged_follows_next_token_from_v2_body(monkeypatch, tmp_path) -> None:
    """WHOOP v2 returns `next_token` in the body; every page must be fetched."""
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"access_token": "t", "expires_at": time.time() + 3600}), encoding="utf-8")
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if "nextToken" not in params:
            return httpx.Response(200, json={"records": [{"id": "a"}], "next_token": "p2"})
        return httpx.Response(200, json={"records": [{"id": "b"}], "next_token": None})

    real_client = httpx.Client
    monkeypatch.setattr(whoop.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    client = whoop.WhoopClient("id", "secret", tokens)
    start = datetime(2026, 7, 31, tzinfo=timezone.utc)
    ids = [r["id"] for r in client.paged("recovery", start, datetime(2026, 9, 27, tzinfo=timezone.utc))]
    assert ids == ["a", "b"]
    assert seen[0]["limit"] == "25"
    assert seen[1]["nextToken"] == "p2"

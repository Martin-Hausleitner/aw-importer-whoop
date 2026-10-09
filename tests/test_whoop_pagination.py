"""Synthetic request/response regressions; never access a real WHOOP account."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from aw_importer_whoop import sync, whoop
from aw_importer_whoop.state import ImportState

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 2, tzinfo=timezone.utc)
CURSOR = "synthetic-next-page+/="


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    original_client = httpx.Client

    def make(responses):
        pending = list(responses)
        requests = []
        sleeps = []

        def handler(request):
            assert request.method == "GET", "Refresh and writes are forbidden in these tests"
            assert request.url.host == "api.prod.whoop.com"
            assert request.headers["Authorization"] == "Bearer synthetic-test-token"
            requests.append(request)
            assert pending, "Unexpected extra request"
            status, payload, headers = pending.pop(0)
            return httpx.Response(status, json=payload, headers=headers)

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            whoop.httpx,
            "Client",
            lambda **kwargs: original_client(transport=transport, **kwargs),
        )
        monkeypatch.setattr(whoop.time, "sleep", sleeps.append)
        token_path = tmp_path / "absent-synthetic-tokens.json"
        client = whoop.WhoopClient("synthetic-client-id", "synthetic-client-secret", token_path)
        client.tokens = {
            "access_token": "synthetic-test-token",
            "expires_at": time.time() + 3600,
        }
        monkeypatch.setattr(
            client, "_save_tokens", lambda: pytest.fail("Token persistence is forbidden")
        )
        return client, requests, pending, sleeps

    return make


@pytest.mark.parametrize("data_type", ("sleep", "workout", "cycle", "recovery"))
@pytest.mark.parametrize("empty_first_page", (False, True))
def test_response_next_token_is_sent_as_nextToken_with_same_filters(
    client_factory, data_type, empty_first_page
):
    first_records = [] if empty_first_page else [{"id": "synthetic-first"}]
    second_records = [{"id": "synthetic-second"}]
    client, requests, pending, sleeps = client_factory([
        (200, {"records": first_records, "next_token": CURSOR}, {}),
        (200, {"records": second_records, "next_token": None}, {}),
    ])

    assert list(client.paged(data_type, START, END)) == first_records + second_records
    assert len(requests) == 2
    for request in requests:
        assert request.url.path == "/developer" + whoop.ENDPOINTS[data_type]
        assert request.url.params["start"] == START.isoformat()
        assert request.url.params["end"] == END.isoformat()
    assert dict(requests[0].url.params) == {
        "start": START.isoformat(), "end": END.isoformat()
    }
    assert dict(requests[1].url.params) == {
        "start": START.isoformat(), "end": END.isoformat(), "nextToken": CURSOR
    }
    assert pending == []
    assert sleeps == []


@pytest.mark.parametrize("cursor_fields", ({}, {"next_token": None}, {"next_token": ""}))
def test_absent_or_empty_response_cursor_finishes_without_extra_request(
    client_factory, cursor_fields
):
    client, requests, pending, sleeps = client_factory([
        (200, {"records": [], **cursor_fields}, {}),
    ])
    assert list(client.paged("cycle", START, END)) == []
    assert len(requests) == 1
    assert pending == []
    assert sleeps == []


@pytest.mark.parametrize("on_second_page", (False, True))
def test_unauthorized_response_raises_without_retry_or_refresh(client_factory, on_second_page):
    responses = []
    if on_second_page:
        responses.append((200, {"records": [], "next_token": CURSOR}, {}))
    responses.append((401, {"error": "synthetic-unauthorized"}, {}))
    client, requests, pending, sleeps = client_factory(responses)

    with pytest.raises(httpx.HTTPStatusError) as error:
        list(client.paged("cycle", START, END))
    assert error.value.response.status_code == 401
    assert len(requests) == (2 if on_second_page else 1)
    assert pending == []
    assert sleeps == []


def test_transient_rate_limit_retries_same_page_then_follows_next_token(client_factory):
    client, requests, pending, sleeps = client_factory([
        (429, {"error": "synthetic-rate-limit"}, {"Retry-After": "2"}),
        (200, {"records": [{"id": "synthetic-first"}], "next_token": CURSOR}, {}),
        (429, {"error": "synthetic-rate-limit"}, {"Retry-After": "3"}),
        (200, {"records": [{"id": "synthetic-second"}], "next_token": None}, {}),
    ])

    assert list(client.paged("cycle", START, END)) == [
        {"id": "synthetic-first"}, {"id": "synthetic-second"}
    ]
    assert len(requests) == 4
    assert requests[0].url == requests[1].url
    assert requests[2].url == requests[3].url
    assert requests[2].url.params["nextToken"] == CURSOR
    assert sleeps == [2, 3]
    assert pending == []


def test_four_rate_limits_raise_without_advancing_or_saving_sync_cursor(
    client_factory, monkeypatch, tmp_path
):
    client, requests, pending, sleeps = client_factory([
        (429, {"error": "synthetic-rate-limit"}, {"Retry-After": "2"})
        for _ in range(4)
    ])
    previous = START.isoformat()
    state = ImportState(last_successful_sync_per_type={"cycle": previous})
    saves = []
    monkeypatch.setattr(state, "save", saves.append)
    monkeypatch.setattr(sync, "state_path", lambda: tmp_path / "absent-state.json")
    loop = sync.SyncLoop.__new__(sync.SyncLoop)
    loop.data_types = ("cycle",)
    loop.whoop = client
    loop.state = state
    loop.aw = SimpleNamespace(
        replace_record_event=lambda *args: pytest.fail("No ActivityWatch write is expected")
    )

    with pytest.raises(httpx.HTTPStatusError) as error:
        loop.tick()
    assert error.value.response.status_code == 429
    assert len(requests) == 4
    assert all(request.url == requests[0].url for request in requests)
    assert sleeps == [2, 2, 2]
    assert pending == []
    assert state.last_successful_sync_per_type == {"cycle": previous}
    assert saves == []
    assert not (tmp_path / "absent-state.json").exists()

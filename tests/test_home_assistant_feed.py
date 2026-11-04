"""Synthetic official-record and local transport tests; never read a real account."""

from __future__ import annotations

import copy
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from aw_importer_whoop import cli, home_assistant as ha, sync
from aw_importer_whoop.activitywatch import EVENT_DATA_SCHEMA_VERSION, stable_hash
from aw_importer_whoop.state import ImportState

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def private_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


def record(kind="cycle", *, user=12345, start="2026-11-04T09:00:00Z", updated="2026-11-04T11:00:00Z", state="SCORED", **extra):
    item = {
        "id": "synthetic-record-id", "user_id": user,
        "start": start, "created_at": start, "end": "2026-11-04T10:00:00Z",
        "updated_at": updated, "score_state": state,
        "score": {"strain": 0, "kilojoule": 100, "average_heart_rate": 60},
    }
    if kind == "recovery":
        item["score"] = {"recovery_score": 0, "resting_heart_rate": 50, "hrv_rmssd_milli": 80}
    if kind == "sleep":
        item["nap"] = False
        item["score"] = {
            "sleep_performance_percentage": 90,
            "sleep_consistency_percentage": 80,
            "sleep_efficiency_percentage": 95,
            "respiratory_rate": 14,
            "stage_summary": {
                "total_in_bed_time_milli": 480 * 60000,
                "total_light_sleep_time_milli": 240 * 60000,
                "total_slow_wave_sleep_time_milli": 90 * 60000,
                "total_rem_sleep_time_milli": 120 * 60000,
            },
        }
    item.update(extra)
    return item


@pytest.fixture
def setup_feed(tmp_path, monkeypatch):
    feed = tmp_path / "feed"
    creds_dir = tmp_path / "ha"
    feed.mkdir(mode=0o700)
    creds_dir.mkdir(mode=0o700)
    creds = creds_dir / "credentials.json"
    config = feed / "config.json"
    private_json(creds, {"url": "http://synthetic-ha.invalid:8123", "token": "synthetic-ha-token"})
    private_json(config, {"source_id": "primary", "ha_credentials_file": str(creds), "bootstrap_days": 2})
    requests = []
    clients = []
    response = {"status": 200}
    original_client = httpx.Client

    def handler(request):
        requests.append(request)
        if response.get("error"):
            raise httpx.ConnectError("secret-token https://private.invalid/raw", request=request)
        if "content" in response:
            return httpx.Response(response["status"], headers=response.get("headers"), content=response["content"])
        return httpx.Response(response["status"], headers=response.get("headers"), json=response.get("ack", {"accepted": True, "duplicate": False}))

    def client(**kwargs):
        clients.append(kwargs)
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(ha.httpx, "Client", client)
    opened = []

    def make():
        publisher = ha.HomeAssistantPublisher(config)
        opened.append(publisher)
        return publisher

    fixture = SimpleNamespace(
        feed=feed, creds=creds, config=config, requests=requests, clients=clients,
        response=response, make=make,
    )
    yield fixture
    for publisher in opened:
        publisher.close()


def publish(fixture, kind="cycle", item=None, now=NOW):
    publisher = fixture.make()
    publisher.begin(now)
    publisher.observe(kind, item or record(kind))
    return publisher, publisher.commit()


def test_sends_allowlisted_official_projection_without_ids_or_tokens(setup_feed):
    f = setup_feed
    item = record(private_note="must-not-leave-record", access_token="not-allowed-as-output")
    original = copy.deepcopy(item)
    _, status = publish(f, item=item)
    assert status == "sent"
    assert len(f.requests) == 1
    req = f.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "http://synthetic-ha.invalid:8123/api/whoop_official/primary"
    assert req.headers["Authorization"] == "Bearer synthetic-ha-token"
    body = json.loads(req.content)
    assert set(body) == {"schema_version", "provider", "source_id", "fetched_at", "resources"}
    assert body["provider"] == "whoop_official_v2"
    assert body["resources"]["cycle"]["metrics"]["strain"] == 0
    assert f.clients == [{"timeout": 10.0, "follow_redirects": False, "trust_env": False}]
    assert len(req.content) <= 64 * 1024
    assert item == original
    cache = (f.feed / "cache.json").read_text()
    assert stat_mode(f.feed / "cache.json") == 0o600
    assert "account_hash" not in req.content.decode()
    for forbidden in ["synthetic-record-id", "12345", "must-not-leave-record", "synthetic-ha-token", "access_token", "refresh_token"]:
        assert forbidden not in req.content.decode()
        assert forbidden not in cache


def stat_mode(path):
    return path.stat().st_mode & 0o777


@pytest.mark.parametrize("state", ["PENDING_SCORE", "UNSCORABLE"])
def test_pending_scores_do_not_become_numeric_zero(setup_feed, state):
    f = setup_feed
    _, status = publish(f, item=record(state=state, score={"strain": float("nan")}))
    assert status == "sent"
    resource = json.loads(f.requests[0].content)["resources"]["cycle"]
    assert resource["score_state"] == state
    assert resource["metrics"] == {}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, True, "12"])
def test_invalid_metrics_reject_entire_staged_feed_without_cache(setup_feed, value):
    f = setup_feed
    _, status = publish(f, item=record(score={"strain": value}))
    assert status == "rejected"
    assert f.requests == []
    assert not (f.feed / "cache.json").exists()


def test_sleep_units_and_workout_duration_are_converted_without_raw_payload(setup_feed):
    f = setup_feed
    p = f.make()
    p.begin(NOW)
    p.observe("sleep", record("sleep"))
    p.observe("workout", record("workout"))
    assert p.commit() == "sent"
    resources = json.loads(f.requests[0].content)["resources"]
    sleep = resources["sleep"]["metrics"]
    assert sleep["total_in_bed_minutes"] == 480
    assert sleep["total_asleep_minutes"] == 450
    assert sleep["deep_sleep_minutes"] == 90
    assert sleep["rem_sleep_minutes"] == 120
    assert resources["workout"]["metrics"]["duration_minutes"] == 60
    assert "stage_summary" not in f.requests[0].content.decode()


def test_negative_duration_cannot_be_coerced_to_zero(setup_feed):
    f = setup_feed
    _, status = publish(f, kind="workout", item=record("workout", end="2026-11-04T08:00:00Z"))
    assert status == "rejected"
    assert f.requests == []


def test_nap_does_not_replace_main_sleep_and_period_beats_amendment(setup_feed):
    f = setup_feed
    p = f.make()
    p.begin(NOW)
    p.observe("sleep", record("sleep", score={"sleep_performance_percentage": 92}))
    p.observe("sleep", record("sleep", start="2026-11-04T11:00:00Z", end="2026-11-04T11:30:00Z", nap=True))
    p.observe("sleep", record("sleep", start="2026-11-03T09:00:00Z", updated="2026-11-04T11:59:00Z", score={"sleep_performance_percentage": 10}))
    assert p.commit() == "sent"
    resource = json.loads(f.requests[0].content)["resources"]["sleep"]
    assert resource["metrics"]["sleep_performance_percentage"] == 92
    assert resource["start"] == "2026-11-04T09:00:00.000000Z"
    assert resource["updated_at"] == "2026-11-04T11:00:00.000000Z"


def test_latest_recovery_created_period_outranks_later_amendment(setup_feed):
    f = setup_feed
    p = f.make()
    p.begin(NOW)
    recent = record("recovery")
    old = record("recovery", updated="2026-11-04T11:59:00Z", created_at="2026-11-03T09:00:00Z", score={"recovery_score": 12})
    for item in (recent, old):
        item.pop("start")
        item.pop("end")
        p.observe("recovery", item)
    assert p.commit() == "sent"
    assert json.loads(f.requests[0].content)["resources"]["recovery"]["metrics"]["recovery_score"] == 0


def test_single_and_changed_accounts_reject_without_identifier_logs(setup_feed, caplog):
    f = setup_feed
    p, _ = publish(f)
    before = (f.feed / "cache.json").read_bytes()
    f.requests.clear()
    p.begin(NOW + timedelta(hours=1))
    p.observe("cycle", record(user=98765))
    assert p.commit() == "rejected"
    assert (f.feed / "cache.json").read_bytes() == before
    assert not f.requests
    assert "98765" not in caplog.text
    assert "12345" not in caplog.text


def test_multiple_accounts_in_first_tick_never_publish(setup_feed):
    f = setup_feed
    p = f.make()
    p.begin(NOW)
    p.observe("cycle", record(user=12345))
    p.observe("recovery", record("recovery", user=98765))
    assert p.commit() == "rejected"
    assert not f.requests
    assert not (f.feed / "cache.json").exists()


@pytest.mark.parametrize("change", ["missing_user", "csv", "naive_time"])
def test_official_provenance_and_timezone_are_required(setup_feed, change):
    f = setup_feed
    item = record()
    if change == "missing_user":
        item.pop("user_id")
    elif change == "csv":
        item["source"] = "whoop_export_csv"
    else:
        item["start"] = "2026-11-04T09:00:00"
    _, status = publish(f, item=item)
    assert status == "rejected"
    assert not f.requests


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_transport_failures_retain_private_pending_cache_without_redirects(setup_feed, status, caplog):
    f = setup_feed
    f.response.update(status=status, headers={"Location": "https://another.invalid/private"})
    caplog.set_level(logging.DEBUG)
    p, result = publish(f)
    assert result == "pending"
    assert len(f.requests) == 1
    cache = json.loads((f.feed / "cache.json").read_text())
    assert cache["pending"] is True
    first_measurement = cache["resources"]["cycle"]["updated_at"]
    f.response["status"] = 200
    p.begin(NOW + timedelta(hours=1))
    assert p.commit() == "sent"
    assert len(f.requests) == 2
    later = json.loads(f.requests[-1].content)
    assert later["resources"]["cycle"]["updated_at"] == first_measurement
    assert later["fetched_at"] != json.loads(f.requests[0].content)["fetched_at"]
    assert "synthetic-ha.invalid" not in caplog.text
    assert "synthetic-ha-token" not in caplog.text
    assert "another.invalid" not in caplog.text


def test_network_exception_is_constant_only(setup_feed, caplog):
    f = setup_feed
    f.response["error"] = True
    _, status = publish(f)
    assert status == "pending"
    assert "ha_feed=delivery_pending" in caplog.text
    assert "secret-token" not in caplog.text
    assert "private.invalid" not in caplog.text


@pytest.mark.parametrize("ack", [
    {"accepted": False}, {"ok": True}, {"accepted": 1},
    {"accepted": True, "duplicate": "yes"},
    {"accepted": True, "raw": "not-allowed"}, [],
])
def test_http_success_requires_bound_handler_ack(setup_feed, ack):
    f = setup_feed
    f.response["ack"] = ack
    _, status = publish(f)
    assert status == "pending"
    assert json.loads((f.feed / "cache.json").read_text())["pending"] is True
    assert len(f.requests) == 1


@pytest.mark.parametrize("content", [b"<html>login</html>", b"{bad-json", b'{"accepted":true,"accepted":false}', b"x" * 8193])
def test_html_malformed_duplicate_or_oversized_ack_stays_pending(setup_feed, content, caplog):
    f = setup_feed
    f.response["content"] = content
    _, status = publish(f)
    assert status == "pending"
    assert json.loads((f.feed / "cache.json").read_text())["pending"] is True
    assert "login" not in caplog.text
    assert "bad-json" not in caplog.text


@pytest.mark.parametrize("kind,score", [
    ("cycle", {"average_heart_rate": 301}),
    ("cycle", {"kilojoule": 10000001}),
    ("recovery", {"hrv_rmssd_milli": 1001}),
    ("recovery", {"skin_temp_celsius": -51}),
    ("sleep", {"respiratory_rate": 101}),
    ("sleep", {"stage_summary": {"total_in_bed_time_milli": 10081 * 60000}}),
])
def test_metrics_match_receiving_transport_bounds(setup_feed, kind, score):
    _, status = publish(setup_feed, kind=kind, item=record(kind, score=score))
    assert status == "rejected"
    assert not setup_feed.requests


@pytest.mark.parametrize("steps", [0, 8234, None])
def test_cycle_steps_use_official_top_level_nullable_field(setup_feed, steps):
    f = setup_feed
    item = record(step_count=steps)
    item["score"]["step_count"] = 999999
    _, status = publish(f, item=item)
    assert status == "sent"
    metrics = json.loads(f.requests[0].content)["resources"]["cycle"]["metrics"]
    if steps is None:
        assert "step_count" not in metrics
    else:
        assert metrics["step_count"] == steps


def test_cycle_fractional_steps_are_rejected(setup_feed):
    _, status = publish(setup_feed, item=record(step_count=0.5))
    assert status == "rejected"


def test_future_resource_does_not_freshen_projection(setup_feed):
    _, status = publish(setup_feed, item=record(updated="2026-11-04T12:01:01Z"))
    assert status == "rejected"
    assert not setup_feed.requests


@pytest.mark.parametrize("case", ["updated_at", "start", "end", "created_rank"])
def test_future_timestamp_never_poison_cache_or_block_next_regular_tick(setup_feed, case):
    f = setup_feed
    p, _ = publish(f)
    before = (f.feed / "cache.json").read_bytes()
    f.requests.clear()
    item = record()
    if case == "created_rank":
        item.pop("updated_at")
        item["created_at"] = "2026-11-06T09:00:00Z"
    else:
        item[case] = "2026-11-06T09:00:00Z"
    p.begin(NOW + timedelta(hours=1))
    p.observe("cycle", item)
    assert p.commit() == "rejected"
    assert (f.feed / "cache.json").read_bytes() == before
    assert not f.requests
    p.begin(NOW + timedelta(hours=2))
    p.observe("cycle", record(updated="2026-11-04T13:30:00Z", score={"strain": 12}))
    assert p.commit() == "sent"
    assert json.loads(f.requests[0].content)["resources"]["cycle"]["metrics"]["strain"] == 12


def test_invalid_sleep_stage_total_is_rejected(setup_feed):
    item = record("sleep")
    item["score"]["stage_summary"]["total_in_bed_time_milli"] = 30 * 60000
    _, status = publish(setup_feed, kind="sleep", item=item)
    assert status == "rejected"


def test_cache_boolean_schema_is_not_integer_version(setup_feed):
    f = setup_feed
    p, _ = publish(f)
    p.close()
    data = json.loads((f.feed / "cache.json").read_text())
    data["cache_schema"] = True
    private_json(f.feed / "cache.json", data)
    with pytest.raises(ha.FeedError, match="cache_schema_invalid"):
        f.make()


@pytest.mark.parametrize("case", ["resource", "rank"])
def test_cache_load_rejects_future_resource_or_hidden_rank(setup_feed, case):
    f = setup_feed
    p, _ = publish(f)
    p.close()
    data = json.loads((f.feed / "cache.json").read_text())
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    if case == "resource":
        data["resources"]["cycle"]["updated_at"] = future
    else:
        data["ranks"]["cycle"][1] = future
    private_json(f.feed / "cache.json", data)
    with pytest.raises(ha.FeedError, match="resource_future"):
        f.make()


def test_cache_is_not_committed_before_complete_tick(setup_feed):
    f = setup_feed
    p, _ = publish(f)
    before = (f.feed / "cache.json").read_bytes()
    f.requests.clear()
    p.begin(NOW + timedelta(hours=1))
    p.observe("cycle", record(score={"strain": 10}))
    p.abort()
    assert p.commit() == "rejected"
    assert (f.feed / "cache.json").read_bytes() == before
    assert not f.requests


def test_bootstrap_is_bounded_and_completed_once_even_without_records(setup_feed):
    f = setup_feed
    p = f.make()
    ordinary = NOW - timedelta(hours=6)
    assert p.start_for(ordinary, NOW) == NOW - timedelta(days=2)
    p.begin(NOW)
    assert p.commit() == "empty"
    assert p.start_for(ordinary, NOW) == ordinary
    assert not f.requests
    assert json.loads((f.feed / "cache.json").read_text())["bootstrap_complete"] is True


def test_second_publisher_lock_is_exclusive_and_released(setup_feed):
    f = setup_feed
    first = f.make()
    with pytest.raises(ha.PublisherBusy):
        ha.HomeAssistantPublisher(f.config)
    first.close()
    f.make()
    assert stat_mode(f.feed / "publisher.lock") == 0o600


@pytest.mark.parametrize("bad", ["config_mode", "parent_mode", "config_hardlink", "credential_symlink", "ancestor_symlink"])
def test_private_files_reject_unsafe_custody(setup_feed, bad, tmp_path):
    f = setup_feed
    path = f.config
    if bad == "config_mode":
        f.config.chmod(0o644)
    elif bad == "parent_mode":
        f.feed.chmod(0o755)
    elif bad == "config_hardlink":
        os.link(f.config, f.feed / "linked.json")
    elif bad == "credential_symlink":
        original = f.creds.with_name("real.json")
        f.creds.rename(original)
        f.creds.symlink_to(original)
    else:
        link = tmp_path / "alias"
        link.symlink_to(f.feed, target_is_directory=True)
        path = link / "config.json"
    with pytest.raises((ha.FeedError, OSError)):
        ha.HomeAssistantPublisher(path)
    assert not f.requests


def test_parent_replacement_and_cache_external_write_reject(setup_feed, tmp_path):
    f = setup_feed
    p, _ = publish(f)
    backup = tmp_path / "old-feed"
    f.feed.rename(backup)
    f.feed.mkdir(mode=0o700)
    private_json(f.config, {"source_id": "primary", "ha_credentials_file": str(f.creds), "bootstrap_days": 2})
    with pytest.raises(ha.FeedError):
        p.begin(NOW + timedelta(hours=1))
    assert not (f.feed / "cache.json").exists()
    assert (backup / "cache.json").is_file()


def test_atomic_fsync_failure_preserves_previous_cache(setup_feed, monkeypatch):
    f = setup_feed
    p, _ = publish(f)
    before = (f.feed / "cache.json").read_bytes()
    f.requests.clear()
    p.begin(NOW + timedelta(hours=1))
    p.observe("cycle", record(score={"strain": 12}))
    monkeypatch.setattr(ha.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("synthetic")))
    assert p.commit() == "pending"
    assert (f.feed / "cache.json").read_bytes() == before
    assert not f.requests
    assert not list(f.feed.glob(".cache-*.tmp"))


def test_changed_credential_origin_does_not_receive_projection(setup_feed):
    f = setup_feed
    p = f.make()
    private_json(f.creds, {"url": "https://another.invalid", "token": "synthetic-new-token"})
    p.begin(NOW)
    p.observe("cycle", record())
    assert p.commit() == "pending"
    assert not f.requests


def test_missing_default_config_does_not_enable_or_create_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(ha, "config_dir", lambda: tmp_path)
    assert ha.optional_publisher() is None
    assert not (tmp_path / "home-assistant-feed").exists()


def test_bad_explicit_config_is_constant_log_only(setup_feed, caplog):
    f = setup_feed
    private_json(f.config, {"source_id": "secret-source https://private.invalid"})
    assert ha.optional_publisher(f.config) is None
    assert "ha_feed=config_rejected" in caplog.text
    assert "secret-source" not in caplog.text
    assert "private.invalid" not in caplog.text


def test_existing_default_config_with_missing_credentials_logs_safe_failure(setup_feed, monkeypatch, caplog):
    f = setup_feed
    private_json(f.config, {
        "source_id": "primary", "ha_credentials_file": str(f.creds.parent / "missing.json"),
        "bootstrap_days": 2,
    })
    f.feed.rename(f.feed.parent / "home-assistant-feed")
    monkeypatch.setattr(ha, "config_dir", lambda: f.feed.parent)
    assert ha.optional_publisher() is None
    assert "ha_feed=config_rejected" in caplog.text
    assert "missing.json" not in caplog.text


def synthetic_loop(publisher, monkeypatch, tmp_path, records, *, fail_kind=None):
    loop = sync.SyncLoop.__new__(sync.SyncLoop)
    loop.data_types = sync.DATA_TYPES
    loop.ha_publisher = publisher
    loop.state = ImportState()
    loop.stop = False
    loop.interval = 3600
    saves = []
    loop.state.save = saves.append
    queries = []

    def paged(kind, start, end):
        queries.append((kind, start, end))
        if kind == fail_kind:
            raise RuntimeError("synthetic provider failure")
        yield from records.get(kind, [])

    loop.whoop = SimpleNamespace(paged=paged)
    loop.aw = SimpleNamespace(replace_record_event=lambda *args: 1)
    monkeypatch.setattr(sync, "utcnow", lambda: NOW)
    monkeypatch.setattr(sync, "state_path", lambda: tmp_path / "provider-state.json")
    return loop, queries, saves


def test_skipped_aw_record_still_contributes_official_feed(setup_feed, monkeypatch, tmp_path):
    f = setup_feed
    p = f.make()
    item = record()
    loop, queries, saves = synthetic_loop(p, monkeypatch, tmp_path, {"cycle": [item]})
    loop.state.record_hashes["cycle:synthetic-record-id"] = stable_hash({"event_data_schema_version": EVENT_DATA_SCHEMA_VERSION, "record": item})
    loop.aw.replace_record_event = lambda *args: pytest.fail("Unchanged AW event must not be rewritten")
    stats = loop.tick()
    assert stats.skipped == 1
    assert len(f.requests) == 1
    assert len(queries) == 4
    assert len(saves) == 1


def test_incomplete_provider_tick_never_sends_or_advances_feed(setup_feed, monkeypatch, tmp_path):
    f = setup_feed
    p, _ = publish(f)
    before = (f.feed / "cache.json").read_bytes()
    f.requests.clear()
    loop, queries, saves = synthetic_loop(p, monkeypatch, tmp_path, {"cycle": [record()], "recovery": []}, fail_kind="recovery")
    with pytest.raises(RuntimeError, match="synthetic provider"):
        loop.tick()
    assert len(queries) == 4
    assert saves == []
    assert not f.requests
    assert (f.feed / "cache.json").read_bytes() == before


def test_provider_save_failure_never_sends_feed(setup_feed, monkeypatch, tmp_path):
    f = setup_feed
    p = f.make()
    loop, _, _ = synthetic_loop(p, monkeypatch, tmp_path, {"cycle": [record()]})
    loop.state.save = lambda _: (_ for _ in ()).throw(OSError("synthetic save failure"))
    with pytest.raises(OSError):
        loop.tick()
    assert not f.requests
    assert not (f.feed / "cache.json").exists()


def test_ha_failure_keeps_provider_success_and_regular_cadence(setup_feed, monkeypatch, tmp_path):
    f = setup_feed
    f.response["status"] = 500
    p = f.make()
    loop, queries, saves = synthetic_loop(p, monkeypatch, tmp_path, {"cycle": [record()]})
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        loop.stop = True

    monkeypatch.setattr(loop, "_sleep", sleep)
    loop.run()
    assert len(queries) == 4
    assert len(saves) == 1
    assert loop.state.last_successful_sync_per_type["cycle"] == NOW.isoformat()
    assert len(sleeps) == 1 and 3590 <= sleeps[0] <= 3600
    assert json.loads((f.feed / "cache.json").read_text())["pending"] is True


def test_selective_provider_import_cannot_advance_full_feed_clock(setup_feed, monkeypatch, tmp_path):
    f = setup_feed
    p, _ = publish(f)
    before = (f.feed / "cache.json").read_bytes()
    f.requests.clear()
    loop, queries, saves = synthetic_loop(p, monkeypatch, tmp_path, {"cycle": [record()]})
    loop.data_types = ("cycle",)
    loop.tick()
    assert len(queries) == 1 and len(saves) == 1
    assert not f.requests
    assert (f.feed / "cache.json").read_bytes() == before


def test_cli_publisher_busy_does_not_create_another_provider_owner(monkeypatch):
    def busy(_):
        raise ha.PublisherBusy("publisher_busy")

    monkeypatch.setattr(cli, "optional_publisher", busy)
    monkeypatch.setattr(cli, "SyncLoop", lambda *args, **kwargs: pytest.fail("No competing owner may start"))
    result = CliRunner().invoke(cli.main, ["sync", "--client-id", "synthetic", "--client-secret", "synthetic", "--once"])
    assert result.exit_code != 0
    assert "ha_feed=publisher_busy" in result.output


def test_cli_explicit_config_is_opt_in_and_publisher_closes(monkeypatch, tmp_path):
    seen = []
    publisher = SimpleNamespace(close=lambda: seen.append("closed"))
    monkeypatch.setattr(cli, "optional_publisher", lambda path: seen.append(path) or publisher)
    monkeypatch.setattr(cli, "SyncLoop", lambda *args, **kwargs: SimpleNamespace(run=lambda **kw: seen.append(kwargs["ha_publisher"])))
    path = tmp_path / "private-config.json"
    result = CliRunner().invoke(cli.main, ["sync", "--client-id", "synthetic", "--client-secret", "synthetic", "--once", "--ha-config", str(path)])
    assert result.exit_code == 0
    assert seen == [path, publisher, "closed"]

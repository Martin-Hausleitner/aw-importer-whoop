"""Opt-in projection of successful official WHOOP ticks; no OAuth or AW reads."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import httpx

from .config import config_dir

log = logging.getLogger("aw-importer-whoop.home_assistant")
PROVIDER = "whoop_official_v2"
MAX_FRAME_BYTES = 64 * 1024
MAX_CACHE_BYTES = 96 * 1024
SOURCE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z", re.ASCII)
SCORE_STATES = {"SCORED", "PENDING_SCORE", "UNSCORABLE"}
# Transport sanity bounds shared with the receiving integration, not diagnoses.
METRIC_BOUNDS = {
    "recovery_score": (0, 100), "resting_heart_rate": (0, 300),
    "hrv_rmssd_milli": (0, 1000), "spo2_percentage": (0, 100),
    "skin_temp_celsius": (-50, 60), "strain": (0, 21),
    "kilojoule": (0, 10000000), "average_heart_rate": (0, 300),
    "max_heart_rate": (0, 300), "step_count": (0, 10000000),
    "sleep_performance_percentage": (0, 100),
    "sleep_consistency_percentage": (0, 100),
    "sleep_efficiency_percentage": (0, 100), "respiratory_rate": (0, 100),
    "total_in_bed_minutes": (0, 10080), "total_asleep_minutes": (0, 10080),
    "deep_sleep_minutes": (0, 10080), "rem_sleep_minutes": (0, 10080),
    "duration_minutes": (0, 10080),
}
METRICS = {
    "recovery": {
        "recovery_score", "resting_heart_rate", "hrv_rmssd_milli",
        "spo2_percentage", "skin_temp_celsius",
    },
    "cycle": {
        "strain", "kilojoule", "average_heart_rate", "max_heart_rate", "step_count",
    },
    "sleep": {
        "sleep_performance_percentage", "sleep_consistency_percentage",
        "sleep_efficiency_percentage", "respiratory_rate", "total_in_bed_minutes",
        "total_asleep_minutes", "deep_sleep_minutes", "rem_sleep_minutes",
    },
    "workout": {
        "strain", "kilojoule", "average_heart_rate", "max_heart_rate", "duration_minutes",
    },
}
_QUIET_HTTP = ContextVar("whoop_ha_quiet_http", default=False)


class FeedError(ValueError):
    """Only constant error codes may leave this module."""


class PublisherBusy(FeedError):
    """A competing publisher must not start another provider owner."""


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _private_regular(st: os.stat_result) -> bool:
    return (
        stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
        and stat.S_IMODE(st.st_mode) == 0o600 and st.st_nlink == 1
    )


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _open_private_parent(path: Path) -> int:
    """Walk every ancestor with NOFOLLOW; require a private direct parent."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise FeedError("private_parent_invalid")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FeedError("json_duplicate_key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise FeedError("non_finite_json")


def _read_at(parent: int, name: str, limit: int) -> tuple[dict[str, Any], os.stat_result]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not _private_regular(before) or not 0 < before.st_size <= limit:
        raise FeedError("private_file_invalid")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        opened = os.fstat(fd)
        chunks = bytearray()
        while len(chunks) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            len(chunks) > limit or not _private_regular(opened)
            or _identity(before) != _identity(opened)
            or _identity(opened) != _identity(after)
            or _identity(after) != _identity(current)
        ):
            raise FeedError("private_file_changed")
        data = json.loads(
            chunks, object_pairs_hook=_json_object, parse_constant=_invalid_constant,
        )
        if not isinstance(data, dict):
            raise FeedError("json_object_required")
        return data, current
    finally:
        os.close(fd)


def _read_private(path: Path, limit: int) -> dict[str, Any]:
    parent = _open_private_parent(path)
    try:
        return _read_at(parent, path.name, limit)[0]
    finally:
        os.close(parent)


def _stamp(value: Any) -> tuple[str, datetime]:
    if not isinstance(value, str) or len(value) > 64 or not re.match(r"^\d{4}-\d{2}-\d{2}T", value):
        raise FeedError("timestamp_invalid")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise FeedError("timestamp_invalid") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise FeedError("timestamp_timezone_required")
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z"), dt


def _now_stamp(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise FeedError("fetch_timezone_required")
    return _stamp(now.isoformat())[0]


def _number(key: str, value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise FeedError("metric_invalid")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (key != "skin_temp_celsius" and value < 0):
        raise FeedError("metric_invalid")
    if key in METRIC_BOUNDS and not METRIC_BOUNDS[key][0] <= value <= METRIC_BOUNDS[key][1]:
        raise FeedError("metric_invalid")
    if key == "step_count" and not isinstance(value, int):
        raise FeedError("metric_invalid")
    return value


def _sleep_consistent(metrics: dict[str, Any]) -> None:
    asleep, in_bed = metrics.get("total_asleep_minutes"), metrics.get("total_in_bed_minutes")
    deep, rem = metrics.get("deep_sleep_minutes"), metrics.get("rem_sleep_minutes")
    if asleep is not None and (
        (in_bed is not None and asleep > in_bed + 1e-8)
        or any(part is not None and part > asleep + 1e-8 for part in (deep, rem))
        or (deep is not None and rem is not None and deep + rem > asleep + 1e-8)
    ):
        raise FeedError("sleep_duration_invalid")


def _not_future(resource: dict[str, Any], rank: list[str] | tuple[str, str], now: datetime) -> None:
    ceiling = now + timedelta(seconds=60)
    values = [resource[key] for key in ("start", "end", "updated_at") if key in resource]
    if any(_stamp(value)[1] > ceiling for value in values + list(rank)):
        raise FeedError("resource_future")


def _project(kind: str, record: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, str]] | None:
    if kind not in METRICS or not isinstance(record, dict):
        raise FeedError("resource_invalid")
    source = record.get("source")
    if isinstance(source, str) and ("export" in source.lower() or "csv" in source.lower()):
        raise FeedError("official_provenance_required")
    if kind == "sleep":
        if "nap" in record and not isinstance(record["nap"], bool):
            raise FeedError("nap_flag_invalid")
        if record.get("nap") is True:
            return None
    state = record.get("score_state")
    if state not in SCORE_STATES:
        raise FeedError("score_state_invalid")
    period = _stamp(record.get("start") or record.get("created_at"))[0]
    updated = _stamp(record.get("updated_at") or record.get("created_at") or record.get("start"))[0]
    result: dict[str, Any] = {"score_state": state, "metrics": {}}
    for key in ("updated_at", "start", "end"):
        if record.get(key) is not None:
            result[key] = _stamp(record[key])[0]
    if not any(k in result for k in ("updated_at", "start", "end")):
        raise FeedError("measurement_timestamp_required")
    start = _stamp(record["start"])[1] if record.get("start") is not None else None
    end = _stamp(record["end"])[1] if record.get("end") is not None else None
    if start is not None and end is not None and end < start:
        raise FeedError("duration_invalid")
    if state != "SCORED":
        return result, (period, updated)
    score = record.get("score")
    if score is not None and not isinstance(score, dict):
        raise FeedError("score_invalid")
    score = score or {}
    metrics = result["metrics"]
    for key in METRICS[kind]:
        raw = record.get(key) if kind == "cycle" and key == "step_count" else score.get(key)
        value = _number(key, raw)
        if value is not None:
            metrics[key] = value
    if kind == "workout" and start is not None and end is not None:
        metrics["duration_minutes"] = _number("duration_minutes", (end - start).total_seconds() / 60)
    if kind == "sleep":
        stages = score.get("stage_summary")
        if stages is not None and not isinstance(stages, dict):
            raise FeedError("sleep_stages_invalid")
        stages = stages or {}
        for source_key, target in (
            ("total_in_bed_time_milli", "total_in_bed_minutes"),
            ("total_slow_wave_sleep_time_milli", "deep_sleep_minutes"),
            ("total_rem_sleep_time_milli", "rem_sleep_minutes"),
        ):
            value = _number("duration_milliseconds", stages.get(source_key))
            if value is not None:
                metrics[target] = _number(target, value / 60000)
        asleep_parts = (
            "total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli",
            "total_rem_sleep_time_milli",
        )
        parts = [_number("duration_milliseconds", stages.get(key)) for key in asleep_parts]
        if all(value is not None for value in parts):
            metrics["total_asleep_minutes"] = _number("total_asleep_minutes", sum(parts) / 60000)
        _sleep_consistent(metrics)
    return result, (period, updated)


class _HideHTTPLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            _QUIET_HTTP.get() and record.name.split(".")[0] in {"httpx", "httpcore"}
        )


@contextmanager
def _quiet_http_logs() -> Iterator[None]:
    # Handler filtering also covers dynamically created httpcore child loggers.
    handlers = set(logging.getLogger().handlers)
    for name, logger in logging.Logger.manager.loggerDict.items():
        if isinstance(logger, logging.Logger) and name.split(".")[0] in {"httpx", "httpcore"}:
            handlers.update(logger.handlers)
    filter_ = _HideHTTPLogs()
    for handler in handlers:
        handler.addFilter(filter_)
    token = _QUIET_HTTP.set(True)
    try:
        yield
    finally:
        _QUIET_HTTP.reset(token)
        for handler in handlers:
            handler.removeFilter(filter_)


class HomeAssistantPublisher:
    """One private publisher, called synchronously by the existing provider loop."""

    def __init__(self, path: Path):
        self.path = _absolute(path)
        self._parent = -1
        self._lock = -1
        self._active = False
        try:
            self._parent = _open_private_parent(self.path)
            self._parent_stat = os.fstat(self._parent)
            config, self._config_stat = _read_at(self._parent, self.path.name, 8192)
            if set(config) != {"source_id", "ha_credentials_file", "bootstrap_days"}:
                raise FeedError("config_schema_invalid")
            self.source_id = config["source_id"]
            days = config["bootstrap_days"]
            if not isinstance(self.source_id, str) or not SOURCE_PATTERN.fullmatch(self.source_id):
                raise FeedError("source_id_invalid")
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 2:
                raise FeedError("bootstrap_days_invalid")
            self.bootstrap_days = days
            reference = config["ha_credentials_file"]
            if not isinstance(reference, str) or not (reference.startswith("/") or reference.startswith("~/")):
                raise FeedError("credential_reference_invalid")
            self.credentials_path = _absolute(reference)
            self._origin, _ = self._credentials()
            self._acquire_lock()
            self._cache_stat = None
            self._cache = self._load_cache()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for name in ("_lock", "_parent"):
            fd = getattr(self, name, -1)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)
        self._active = False

    def _credentials(self) -> tuple[str, str]:
        data = _read_private(self.credentials_path, 16384)
        if set(data) != {"url", "token"}:
            raise FeedError("credential_schema_invalid")
        origin, token = data["url"], data["token"]
        if not isinstance(origin, str) or not isinstance(token, str) or not token or len(token) > 12000:
            raise FeedError("credential_schema_invalid")
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or any(c in token or c in origin for c in ("\r", "\n"))
        ):
            raise FeedError("credential_origin_invalid")
        # Keep the stored origin; only the endpoint separator is appended later.
        return origin, token

    def _acquire_lock(self) -> None:
        self._lock = os.open(
            "publisher.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
            dir_fd=self._parent,
        )
        st = os.fstat(self._lock)
        current = os.stat("publisher.lock", dir_fd=self._parent, follow_symlinks=False)
        if not _private_regular(st) or _identity(st) != _identity(current):
            raise FeedError("publisher_lock_invalid")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublisherBusy("publisher_busy") from None
        self._lock_stat = st

    def _guard(self) -> None:
        fresh = _open_private_parent(self.path)
        try:
            if (os.fstat(fresh).st_dev, os.fstat(fresh).st_ino) != (
                self._parent_stat.st_dev, self._parent_stat.st_ino,
            ):
                raise FeedError("publisher_parent_changed")
        finally:
            os.close(fresh)
        config = os.stat(self.path.name, dir_fd=self._parent, follow_symlinks=False)
        lock = os.stat("publisher.lock", dir_fd=self._parent, follow_symlinks=False)
        if (
            not _private_regular(config) or _identity(config) != _identity(self._config_stat)
            or not _private_regular(lock) or _identity(lock) != _identity(self._lock_stat)
        ):
            raise FeedError("publisher_generation_changed")

    def _load_cache(self) -> dict[str, Any]:
        try:
            data, self._cache_stat = _read_at(self._parent, "cache.json", MAX_CACHE_BYTES)
        except FileNotFoundError:
            return {
                "cache_schema": 1, "source_id": self.source_id, "provider": PROVIDER,
                "account_hash": None, "resources": {}, "ranks": {},
                "fetched_at": None, "bootstrap_complete": False, "pending": False,
            }
        expected = {
            "cache_schema", "source_id", "provider", "account_hash", "resources",
            "ranks", "fetched_at", "bootstrap_complete", "pending",
        }
        if (
            set(data) != expected or type(data["cache_schema"]) is not int or data["cache_schema"] != 1
            or data["source_id"] != self.source_id or data["provider"] != PROVIDER
            or not isinstance(data["bootstrap_complete"], bool)
            or not isinstance(data["pending"], bool)
        ):
            raise FeedError("cache_schema_invalid")
        account = data["account_hash"]
        if account is not None and (not isinstance(account, str) or not re.fullmatch(r"[a-f0-9]{64}", account)):
            raise FeedError("cache_account_invalid")
        resources, ranks = data["resources"], data["ranks"]
        if not isinstance(resources, dict) or not isinstance(ranks, dict) or set(resources) != set(ranks):
            raise FeedError("cache_resources_invalid")
        if resources and account is None:
            raise FeedError("cache_account_required")
        for kind, resource in resources.items():
            if kind not in METRICS or not isinstance(resource, dict):
                raise FeedError("cache_resource_invalid")
            if set(resource) - {"score_state", "updated_at", "start", "end", "metrics"}:
                raise FeedError("cache_resource_invalid")
            if resource.get("score_state") not in SCORE_STATES or not isinstance(resource.get("metrics"), dict):
                raise FeedError("cache_resource_invalid")
            for key, value in resource["metrics"].items():
                if key not in METRICS[kind] or _number(key, value) is None:
                    raise FeedError("cache_metric_invalid")
            if resource["score_state"] != "SCORED" and resource["metrics"]:
                raise FeedError("cache_pending_metric")
            if not any(key in resource for key in ("start", "end", "updated_at")):
                raise FeedError("cache_measurement_timestamp_required")
            for key in ("start", "end", "updated_at"):
                if key in resource:
                    resource[key] = _stamp(resource[key])[0]
            if "start" in resource and "end" in resource and resource["end"] < resource["start"]:
                raise FeedError("cache_duration_invalid")
            if kind == "sleep":
                _sleep_consistent(resource["metrics"])
            rank = ranks[kind]
            if not isinstance(rank, list) or len(rank) != 2:
                raise FeedError("cache_rank_invalid")
            ranks[kind] = [_stamp(value)[0] for value in rank]
            _not_future(resource, ranks[kind], datetime.now(timezone.utc))
        if data["fetched_at"] is not None:
            _, fetched = _stamp(data["fetched_at"])
            if fetched > datetime.now(timezone.utc) + timedelta(seconds=60):
                raise FeedError("cache_fetch_future")
        return data

    def begin(self, now: datetime) -> None:
        self._guard()
        self._staged = copy.deepcopy(self._cache)
        self._fetch_at = _now_stamp(now)
        self._active = True
        self._valid = True

    def start_for(self, existing: datetime, now: datetime) -> datetime:
        if not self._cache["bootstrap_complete"]:
            return min(existing, now - timedelta(days=self.bootstrap_days))
        return existing

    def observe(self, kind: str, record: dict[str, Any]) -> None:
        if not self._active or not self._valid:
            return
        try:
            user_id = record.get("user_id") if isinstance(record, dict) else None
            if isinstance(user_id, bool) or not isinstance(user_id, int) or not 0 < user_id < 2**63:
                raise FeedError("account_binding_required")
            account = hashlib.sha256(b"whoop-user-v2\x00" + str(user_id).encode("ascii")).hexdigest()
            previous = self._staged["account_hash"]
            if previous is not None and previous != account:
                raise FeedError("account_binding_changed")
            self._staged["account_hash"] = account
            projected = _project(kind, record)
            if projected is None:
                return
            resource, rank = projected
            _not_future(resource, rank, _stamp(self._fetch_at)[1])
            old = self._staged["ranks"].get(kind)
            if old is None or rank >= tuple(old):
                self._staged["resources"][kind] = resource
                self._staged["ranks"][kind] = list(rank)
        except Exception:
            self._valid = False
            log.warning("ha_feed=record_rejected")

    def abort(self) -> None:
        self._active = False

    def _write_cache(self, data: dict[str, Any]) -> None:
        self._guard()
        try:
            current = os.stat("cache.json", dir_fd=self._parent, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if (current is None) != (self._cache_stat is None) or (
            current is not None and (
                not _private_regular(current) or _identity(current) != _identity(self._cache_stat)
            )
        ):
            raise FeedError("cache_generation_changed")
        encoded = json.dumps(data, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_CACHE_BYTES:
            raise FeedError("cache_too_large")
        name = ".cache-" + secrets.token_hex(16) + ".tmp"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._parent)
        created = os.fstat(fd)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise FeedError("cache_write_failed")
                view = view[written:]
            os.fsync(fd)
            os.rename(name, "cache.json", src_dir_fd=self._parent, dst_dir_fd=self._parent)
            os.fsync(self._parent)
            self._cache_stat = os.stat("cache.json", dir_fd=self._parent, follow_symlinks=False)
        finally:
            os.close(fd)
            try:
                remaining = os.stat(name, dir_fd=self._parent, follow_symlinks=False)
                if (remaining.st_dev, remaining.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(name, dir_fd=self._parent)
            except FileNotFoundError:
                pass

    def commit(self) -> str:
        if not self._active or not self._valid:
            self.abort()
            return "rejected"
        self.abort()
        try:
            self._staged["bootstrap_complete"] = True
            self._staged["fetched_at"] = self._fetch_at
            resources = self._staged["resources"]
            for kind, resource in resources.items():
                _not_future(resource, self._staged["ranks"][kind], _stamp(self._fetch_at)[1])
            self._staged["pending"] = bool(resources)
            frame = {
                "schema_version": 1, "provider": PROVIDER, "source_id": self.source_id,
                "fetched_at": self._fetch_at, "resources": resources,
            }
            body = json.dumps(frame, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(body) > MAX_FRAME_BYTES:
                raise FeedError("frame_too_large")
            self._write_cache(self._staged)
            self._cache = copy.deepcopy(self._staged)
            if not resources:
                return "empty"
            origin, token = self._credentials()
            if origin != self._origin:
                raise FeedError("ha_origin_changed")
            self._guard()
            with _quiet_http_logs(), httpx.Client(
                timeout=10.0, follow_redirects=False, trust_env=False,
            ) as client:
                response = client.post(
                    origin.rstrip("/") + "/api/whoop_official/" + self.source_id,
                    content=body,
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                )
            if not 200 <= response.status_code < 300:
                log.warning("ha_feed=delivery_pending")
                return "pending"
            if len(response.content) > 8192:
                raise FeedError("ha_ack_invalid")
            ack = json.loads(
                response.content, object_pairs_hook=_json_object,
                parse_constant=_invalid_constant,
            )
            if (
                not isinstance(ack, dict) or ack.get("accepted") is not True
                or set(ack) - {"accepted", "duplicate"}
                or ("duplicate" in ack and type(ack["duplicate"]) is not bool)
            ):
                raise FeedError("ha_ack_invalid")
            self._cache["pending"] = False
            self._write_cache(self._cache)
            return "sent"
        except Exception:
            log.warning("ha_feed=delivery_pending")
            return "pending"


def optional_publisher(path: Path | None = None) -> HomeAssistantPublisher | None:
    """Missing default config disables the feature; malformed config never logs values."""
    explicit = path is not None
    if path is None:
        path = config_dir() / "home-assistant-feed" / "config.json"
    path = _absolute(path)
    try:
        os.lstat(path)
    except FileNotFoundError:
        if explicit:
            log.warning("ha_feed=config_rejected")
        return None
    except Exception:
        log.warning("ha_feed=config_rejected")
        return None
    try:
        return HomeAssistantPublisher(path)
    except PublisherBusy:
        raise
    except Exception:
        log.warning("ha_feed=config_rejected")
        return None

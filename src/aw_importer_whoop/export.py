from __future__ import annotations

import csv
import gzip
import hashlib
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterable

from dateutil import parser

from .activitywatch import ActivityWatchClient, stable_hash
from .config import state_path
from .state import ImportState

EXPORT_TYPES = ("sleep", "workout", "cycle", "journal")


@dataclass
class ExportStats:
    parsed: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


def _read_zip(path: Path) -> zipfile.ZipFile:
    if zipfile.is_zipfile(path):
        return zipfile.ZipFile(path)
    # WHOOP sometimes downloads through clients as gzip-wrapped bytes; normalize if needed.
    tmpdir = TemporaryDirectory()
    normalized = Path(tmpdir.name) / "whoop-export.zip"
    with gzip.open(path, "rb") as src, normalized.open("wb") as dst:
        dst.write(src.read())
    zf = zipfile.ZipFile(normalized)
    zf._aw_tmpdir = tmpdir  # type: ignore[attr-defined]  # keep tmpdir alive with ZipFile
    return zf


def _parse_dt(value: str | None, tz_hint: str | None = None) -> str | None:
    if not value or not value.strip():
        return None
    text = value.strip()
    # dateutil does not reliably parse strings like UTC+02:00 as a separate hint.
    if tz_hint and tz_hint.strip().upper().startswith("UTC") and "+" not in text and "Z" not in text:
        text = f"{text} {tz_hint.strip().upper().replace('UTC', '')}"
    return parser.parse(text).isoformat()


def _float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in {"1", "y", "yes", "true"}


def _id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return f"export-{prefix}-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _base(row: dict[str, str]) -> dict[str, Any]:
    return {
        "cycle_start": _parse_dt(row.get("Cycle start time"), row.get("Cycle timezone")),
        "cycle_end": _parse_dt(row.get("Cycle end time"), row.get("Cycle timezone")),
        "timezone": row.get("Cycle timezone") or None,
        "source": "whoop_export_csv",
    }


def sleep_records(rows: Iterable[dict[str, str]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        start = _parse_dt(row.get("Sleep onset"), row.get("Cycle timezone"))
        end = _parse_dt(row.get("Wake onset"), row.get("Cycle timezone"))
        if not start:
            continue
        record = _base(row) | {
            "sleep_id": _id("sleep", start, end),
            "start": start,
            "end": end or start,
            "sleep_performance_percent": _float(row.get("Sleep performance %")),
            "respiratory_rate_rpm": _float(row.get("Respiratory rate (rpm)")),
            "asleep_duration_min": _float(row.get("Asleep duration (min)")),
            "in_bed_duration_min": _float(row.get("In bed duration (min)")),
            "light_sleep_duration_min": _float(row.get("Light sleep duration (min)")),
            "deep_sws_duration_min": _float(row.get("Deep (SWS) duration (min)")),
            "rem_duration_min": _float(row.get("REM duration (min)")),
            "nap": _bool(row.get("Nap")),
        }
        yield {k: v for k, v in record.items() if v is not None}


def workout_records(rows: Iterable[dict[str, str]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        start = _parse_dt(row.get("Workout start time"), row.get("Cycle timezone"))
        end = _parse_dt(row.get("Workout end time"), row.get("Cycle timezone"))
        if not start:
            continue
        record = _base(row) | {
            "workout_id": _id("workout", start, end, row.get("Activity name")),
            "start": start,
            "end": end or start,
            "activity_name": row.get("Activity name") or None,
            "duration_min": _float(row.get("Duration (min)")),
            "strain": _float(row.get("Activity Strain")),
            "energy_burned_cal": _float(row.get("Energy burned (cal)")),
            "max_heart_rate": _float(row.get("Max HR (bpm)")),
            "average_heart_rate": _float(row.get("Average HR (bpm)")),
        }
        yield {k: v for k, v in record.items() if v is not None}


def cycle_records(rows: Iterable[dict[str, str]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        start = _parse_dt(row.get("Cycle start time"), row.get("Cycle timezone"))
        end = _parse_dt(row.get("Cycle end time"), row.get("Cycle timezone"))
        if not start:
            continue
        record = _base(row) | {
            "cycle_id": _id("cycle", start, end),
            "start": start,
            "end": end or start,
            "recovery_score_percent": _float(row.get("Recovery score %")),
            "resting_heart_rate_bpm": _float(row.get("Resting heart rate (bpm)")),
            "heart_rate_variability_ms": _float(row.get("Heart rate variability (ms)")),
            "skin_temp_celsius": _float(row.get("Skin temp (celsius)")),
            "blood_oxygen_percent": _float(row.get("Blood oxygen %")),
            "strain": _float(row.get("Day Strain")),
            "energy_burned_cal": _float(row.get("Energy burned (cal)")),
            "max_heart_rate": _float(row.get("Max HR (bpm)")),
            "average_heart_rate": _float(row.get("Average HR (bpm)")),
        }
        yield {k: v for k, v in record.items() if v is not None}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "question"


def journal_records(rows: Iterable[dict[str, str]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        start = _parse_dt(row.get("Cycle start time"), row.get("Cycle timezone"))
        end = _parse_dt(row.get("Cycle end time"), row.get("Cycle timezone")) or start
        question = row.get("Question text") or ""
        if not question:
            continue
        answered = _bool(row.get("Answered yes"))
        record = _base(row) | {
            "id": _id("journal", start, end, question, answered, row.get("Notes")),
            "start": start or _parse_dt(row.get("Cycle end time"), row.get("Cycle timezone")) or "1970-01-01T00:00:00+00:00",
            "end": end or start or "1970-01-01T00:00:00+00:00",
            "question": question,
            "question_slug": _slug(question),
            "answered_yes": answered,
            "has_notes": bool((row.get("Notes") or "").strip()),
        }
        yield {k: v for k, v in record.items() if v is not None}


READERS: dict[str, tuple[str, Callable[[Iterable[dict[str, str]]], Iterable[dict[str, Any]]]]] = {
    "sleep": ("sleeps.csv", sleep_records),
    "workout": ("workouts.csv", workout_records),
    "cycle": ("physiological_cycles.csv", cycle_records),
    "journal": ("journal_entries.csv", journal_records),
}


def import_export(path: Path, data_types: tuple[str, ...] = EXPORT_TYPES, dry_run: bool = False) -> ExportStats:
    stats = ExportStats()
    aw = ActivityWatchClient()
    state = ImportState.load(state_path())

    with _read_zip(path) as zf:
        names = set(zf.namelist())
        for data_type in data_types:
            filename, reader = READERS[data_type]
            if filename not in names:
                continue
            text = zf.read(filename).decode("utf-8-sig")
            rows = csv.DictReader(text.splitlines())
            for record in reader(rows):
                stats.parsed += 1
                rid = f"export:{data_type}:{record.get('id') or record.get('sleep_id') or record.get('workout_id') or record.get('cycle_id')}"
                h = stable_hash(record)
                old = state.record_hashes.get(rid)
                if old == h:
                    stats.skipped += 1
                    continue
                if not dry_run:
                    new_id = aw.replace_record_event(data_type, record, state.aw_event_ids.get(rid))
                    state.record_hashes[rid] = h
                    if new_id >= 0:
                        state.aw_event_ids[rid] = new_id
                if old:
                    stats.updated += 1
                else:
                    stats.inserted += 1
    if not dry_run:
        state.save(state_path())
    return stats

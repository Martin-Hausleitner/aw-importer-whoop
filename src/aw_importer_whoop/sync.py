from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

from .activitywatch import ActivityWatchClient, EVENT_DATA_SCHEMA_VERSION, record_uuid, stable_hash
from .config import DATA_TYPES, DEFAULT_INTERVAL_SECONDS, OVERLAP_SECONDS, state_path, token_path
from .state import ImportState, parse_dt, utcnow
from .whoop import WhoopClient

log = logging.getLogger("aw-importer-whoop")


@dataclass
class TickStats:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


class SyncLoop:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        interval: int = DEFAULT_INTERVAL_SECONDS,
        data_types: Iterable[str] | None = DATA_TYPES,
    ):
        self.interval = interval
        self.data_types = tuple(data_types or DATA_TYPES)
        self.whoop = WhoopClient(client_id, client_secret, token_path())
        self.aw = ActivityWatchClient()
        self.state = ImportState.load(state_path())
        self.stop = False
        signal.signal(signal.SIGINT, self._request_stop)
        signal.signal(signal.SIGTERM, self._request_stop)

    def _request_stop(self, *_: object) -> None:
        self.stop = True

    def _start_for(self, data_type: str):
        last = parse_dt(self.state.last_successful_sync_per_type.get(data_type))
        if last is None:
            return utcnow() - timedelta(days=30)
        return last - timedelta(seconds=OVERLAP_SECONDS)

    def tick(self) -> TickStats:
        stats = TickStats()
        now = utcnow()
        for data_type in self.data_types:
            for record in self.whoop.paged(data_type, self._start_for(data_type), now):
                stats.fetched += 1
                rid = f"{data_type}:{record_uuid(record)}"
                h = stable_hash({"event_data_schema_version": EVENT_DATA_SCHEMA_VERSION, "record": record})
                old = self.state.record_hashes.get(rid)
                if old == h:
                    stats.skipped += 1
                    continue
                new_event_id = self.aw.replace_record_event(data_type, record, self.state.aw_event_ids.get(rid))
                self.state.record_hashes[rid] = h
                if new_event_id >= 0:
                    self.state.aw_event_ids[rid] = new_event_id
                if old:
                    stats.updated += 1
                else:
                    stats.inserted += 1
            # The generator only returns after all pages completed successfully.
            # Advance the cursor even if WHOOP returned zero records, otherwise empty
            # types would refetch the initial 30-day window forever.
            self.state.last_successful_sync_per_type[data_type] = now.isoformat()
        self.state.save(state_path())
        return stats

    def run(self, once: bool = False) -> None:
        backoff = 1
        while not self.stop:
            tick_started = time.time()
            try:
                stats = self.tick()
                backoff = 1
                next_in = 0 if once else max(0, self.interval - int(time.time() - tick_started))
                log.info(
                    "tick=%s fetched=%s inserted=%s updated=%s skipped=%s next_in=%ss",
                    int(tick_started), stats.fetched, stats.inserted, stats.updated, stats.skipped, next_in,
                )
                if once:
                    return
                self._sleep(next_in)
            except Exception as e:  # log and continue on transient errors
                wait = min(backoff, self.interval)
                log.warning("tick_error=%s next_in=%ss", json.dumps(str(e)), wait)
                if once:
                    raise
                self._sleep(wait)
                backoff = min(backoff * 2, self.interval)

    def _sleep(self, seconds: int) -> None:
        end = time.time() + seconds
        while not self.stop and time.time() < end:
            time.sleep(min(1, end - time.time()))

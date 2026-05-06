"""Core sync orchestration logic."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from googleapiclient.errors import HttpError

from .calendar_reader import CalendarReader
from .calendar_writer import CalendarWriter
from .models import EventData, SyncStats
from .state import SyncState
from .types import GoogleEvent

log = logging.getLogger(__name__)


class CalendarSyncer:
    """Orchestrates one-way sync from a source calendar to a destination calendar.

    Matching strategy uses *(title, time range, URL)* rather than identifiers,
    making recurring events work correctly and avoiding state-file lock-in.

    The sync is structured as three sequential passes per run:

    1. **Upsert pass** — ensure every source event has a corresponding destination event.
    2. **Delete pass** — remove destination events whose source counterparts disappeared.
    3. **Verify pass** — re-fetch destination and confirm all source events are visible.
    """

    def __init__(
        self,
        reader: CalendarReader,
        writer: CalendarWriter,
        state: SyncState,
        verify_wait_secs: float = 3.0,
        time_tolerance_secs: float = 120.0,
    ) -> None:
        """Create a ``CalendarSyncer``.

        Args:
            reader:              Configured source calendar reader.
            writer:              Configured destination calendar writer.
            state:               Persistent state manager.
            verify_wait_secs:    Seconds to wait before the verification re-fetch.
            time_tolerance_secs: Maximum allowed start/end delta (seconds).
        """
        self._reader = reader
        self._writer = writer
        self._state = state
        self._verify_wait = verify_wait_secs
        self._tolerance = time_tolerance_secs

    def run(self, start: datetime, end: datetime) -> SyncStats:
        """Execute a full sync cycle for the given time window.

        Args:
            start: Sync window start (inclusive), must be timezone-aware.
            end:   Sync window end (exclusive), must be timezone-aware.

        Returns:
            ``SyncStats`` with per-operation counts and any unresolved events.
        """
        source_events = self._reader.fetch_events(start, end)
        dest_events = self._writer.fetch_events(start, end)
        dest_by_id: dict[str, GoogleEvent] = {ev["id"]: ev for ev in dest_events}
        state_data = self._state.data

        log.info("Source events fetched     : %d", len(source_events))
        log.info("Destination events fetched: %d", len(dest_by_id))
        log.info("State entries loaded      : %d", len(state_data))

        new_state, stats = self._upsert_pass(source_events, dest_by_id, state_data)
        self._delete_pass(state_data, dest_by_id, new_state, stats)
        stats.missing = self._verify_pass(source_events, new_state, start, end)

        self._state.save(new_state)
        return stats

    # ── Sync passes ───────────────────────────────────────────────────────────

    def _upsert_pass(
        self,
        source_events: list[EventData],
        dest_by_id: dict[str, GoogleEvent],
        state_data: dict[str, str],
    ) -> tuple[dict[str, str], SyncStats]:
        """Ensure every source event has a corresponding destination event."""
        new_state: dict[str, str] = {}
        stats = SyncStats()
        dest_list: list[GoogleEvent] = list(dest_by_id.values())

        for ev in source_events:
            stored_id = state_data.get(ev.state_key)
            dest_ev: GoogleEvent | None = (
                dest_by_id.get(stored_id) if stored_id else None
            )

            if dest_ev is None:
                dest_ev = self._find_match(ev, dest_list)

            if dest_ev is not None:
                new_state[ev.state_key] = dest_ev["id"]
                if self._events_differ(ev, dest_ev):
                    try:
                        self._writer.update_event(dest_ev["id"], ev)
                        stats.updated += 1
                        log.info("Updated : %s", ev)
                    except HttpError as exc:
                        log.error("Failed to update %s: %s", ev, exc)
                elif stored_id != dest_ev["id"]:
                    stats.linked += 1
                    log.info("Linked  : %s", ev)
                else:
                    stats.skipped += 1
            else:
                try:
                    new_id = self._writer.create_event(ev)
                    new_state[ev.state_key] = new_id
                    stub: GoogleEvent = {  # type: ignore[typeddict-unknown-key]
                        "id": new_id,
                        "summary": ev.title,
                        "description": ev.url,
                        "start": {"date": ev.start} if ev.all_day else {"dateTime": ev.start},
                        "end": {"date": ev.end} if ev.all_day else {"dateTime": ev.end},
                    }
                    dest_list.append(stub)
                    stats.created += 1
                    log.info("Created : %s", ev)
                except HttpError as exc:
                    log.error("Failed to create %s: %s", ev, exc)

        return new_state, stats

    def _delete_pass(
        self,
        state_data: dict[str, str],
        dest_by_id: dict[str, GoogleEvent],
        new_state: dict[str, str],
        stats: SyncStats,
    ) -> None:
        """Remove destination events whose source counterparts have disappeared."""
        for key, dest_id in state_data.items():
            if key in new_state:
                continue
            dest_ev = dest_by_id.get(dest_id)
            if dest_ev is None:
                continue
            try:
                self._writer.delete_event(dest_id)
                stats.deleted += 1
                log.info("Deleted : %s", dest_ev.get("summary", dest_id))
            except HttpError as exc:
                if exc.resp.status in (404, 410):
                    pass
                else:
                    log.error("Failed to delete %s: %s", dest_id, exc)
                    new_state[key] = dest_id

    def _verify_pass(
        self,
        source_events: list[EventData],
        new_state: dict[str, str],
        start: datetime,
        end: datetime,
    ) -> list[EventData]:
        """Re-fetch destination and confirm every source event is present."""
        log.info("Verification: waiting %.0f s then re-fetching…", self._verify_wait)
        time.sleep(self._verify_wait)

        fresh_events = self._writer.fetch_events(start, end)
        fresh_by_id: dict[str, GoogleEvent] = {ev["id"]: ev for ev in fresh_events}
        fresh_list = list(fresh_events)

        missing: list[EventData] = []
        for ev in source_events:
            dest_id = new_state.get(ev.state_key)
            if dest_id and dest_id in fresh_by_id:
                continue
            if self._find_match(ev, fresh_list) is not None:
                continue
            missing.append(ev)

        return missing

    # ── Matching helpers ──────────────────────────────────────────────────────

    def _find_match(
        self,
        ev: EventData,
        candidates: list[GoogleEvent],
    ) -> GoogleEvent | None:
        """Find a destination event corresponding to the given source event.

        Matching requires: title equality, time equivalence, and (if URL present)
        the URL must appear in the destination event.

        Args:
            ev:         Source event to look up.
            candidates: Destination events to search through.

        Returns:
            Best-matching destination event dict, or ``None`` if no match found.
        """
        source_title = ev.title.strip().lower()

        time_matches = [
            g for g in candidates
            if (g.get("summary") or "").strip().lower() == source_title
            and self._times_match(ev, g)
        ]

        if not time_matches:
            return None

        if not ev.url:
            return time_matches[0]

        url_matches = [g for g in time_matches if ev.url in self._url_haystack(g)]
        if url_matches:
            return url_matches[0]

        return time_matches[0] if len(time_matches) == 1 else None

    def _events_differ(self, ev: EventData, gev: GoogleEvent) -> bool:
        """Return ``True`` if any synced field has changed."""
        if ev.title != (gev.get("summary") or ""):
            return True
        if ev.location != (gev.get("location") or ""):
            return True
        if ev.status != gev.get("status", "confirmed"):
            return True
        if ev.url and ev.url not in self._url_haystack(gev):
            return True

        g_start = gev.get("start", {})
        g_end = gev.get("end", {})

        if ev.all_day:
            return (
                ev.start != g_start.get("date", "")
                or ev.end != g_end.get("date", "")
            )

        a_s = self._parse_utc(ev.start)
        a_e = self._parse_utc(ev.end)
        g_s = self._parse_utc(g_start.get("dateTime", ""))
        g_e = self._parse_utc(g_end.get("dateTime", ""))

        if None in (a_s, a_e, g_s, g_e):
            return True

        return (
            abs((a_s - g_s).total_seconds()) > self._tolerance  # type: ignore[operator]
            or abs((a_e - g_e).total_seconds()) > self._tolerance  # type: ignore[operator]
        )

    def _times_match(self, ev: EventData, gev: GoogleEvent) -> bool:
        """Return ``True`` if event start/end times agree within tolerance."""
        g_start = gev.get("start", {})
        g_end = gev.get("end", {})

        if ev.all_day:
            return (
                ev.start == g_start.get("date", "")
                and ev.end == g_end.get("date", "")
            )

        a_s = self._parse_utc(ev.start)
        a_e = self._parse_utc(ev.end)
        g_s = self._parse_utc(g_start.get("dateTime", ""))
        g_e = self._parse_utc(g_end.get("dateTime", ""))

        if None in (a_s, a_e, g_s, g_e):
            return False

        return (
            abs((a_s - g_s).total_seconds()) <= self._tolerance  # type: ignore[operator]
            and abs((a_e - g_e).total_seconds()) <= self._tolerance  # type: ignore[operator]
        )

    @staticmethod
    def _url_haystack(gev: GoogleEvent) -> str:
        """Concatenate all URL-bearing fields of a destination event for search."""
        parts = [
            gev.get("description") or "",
            gev.get("location") or "",
            gev.get("hangoutLink") or "",
        ]
        for ep in (gev.get("conferenceData") or {}).get("entryPoints", []):
            parts.append(ep.get("uri") or "")
        return "\n".join(parts)

    @staticmethod
    def _parse_utc(s: str) -> datetime | None:
        """Parse an ISO-8601 string and normalise to UTC.

        Args:
            s: Datetime string to parse.

        Returns:
            UTC-normalised ``datetime``, or ``None`` if parsing fails.
        """
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

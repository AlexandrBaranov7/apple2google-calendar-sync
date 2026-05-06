#!/usr/bin/env python3
"""One-way calendar sync: Apple Calendar (Exchange/Outlook) → Google Calendar.

Architecture
------------
``AppleCalendarReader``  reads events from Apple Calendar via EventKit (pyobjc).
``GoogleCalendarClient`` reads and writes events via the Google Calendar REST API.
``SyncState``            persists the ``state_key → google_event_id`` mapping on disk.
``CalendarSyncer``       orchestrates the three-pass sync cycle.
``SyncConfig``           bundles all runtime configuration in a single frozen dataclass.

Matching strategy
-----------------
Events are matched by *(title, time range, URL)* rather than by iCalUID.
Exchange recurring events share one UID across all instances; using ``uid|start``
as the state key makes every occurrence independently addressable.  A content-based
fallback prevents duplicates when the state file is reset or when a previous sync
was only partial.

Sync cycle
----------
1. **Upsert pass** – every Apple event in the window is created or updated in Google.
2. **Delete pass** – events tracked in state but gone from Apple are removed from Google.
3. **Verify pass** – a fresh Google fetch confirms every Apple event is present;
   gaps are collected in ``SyncStats.missing`` and logged as warnings.

Usage::

    python calendar_sync.py   # interactive OAuth on first run, headless afterwards
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Literal, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import objc  # noqa: F401 – initialises the pyobjc runtime
from Foundation import NSDate
import EventKit

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

__all__ = [
    "AppleCalendarReader",
    "GoogleCalendarClient",
    "SyncState",
    "CalendarSyncer",
    "SyncConfig",
    "EventData",
    "SyncStats",
    "CalendarSyncError",
    "CalendarAccessDeniedError",
    "CalendarNotFoundError",
]

log = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────


class CalendarSyncError(Exception):
    """Base class for all calendar sync errors."""


class CalendarAccessDeniedError(CalendarSyncError):
    """Apple Calendar access was denied in System Settings."""


class CalendarNotFoundError(CalendarSyncError):
    """The requested calendar name does not exist in Apple Calendar."""


# ── Google API type stubs ─────────────────────────────────────────────────────


class _TimeSlot(TypedDict, total=False):
    date: str
    dateTime: str
    timeZone: str


class GoogleEvent(TypedDict, total=False):
    """Subset of the Google Calendar event resource used by this module."""

    id: str
    summary: str
    location: str
    description: str
    status: str
    start: _TimeSlot
    end: _TimeSlot
    hangoutLink: str
    conferenceData: dict[str, Any]


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EventData:
    """Immutable snapshot of a single calendar event instance.

    Attributes:
        state_key:  Unique key used in the state file, formed as ``uid|start``.
                    Handles recurring events whose UID is shared across instances.
        uid:        Calendar item external identifier from EventKit.
        title:      Event title / summary.
        location:   Physical or virtual location string (may be empty).
        url:        Meeting URL extracted from the EventKit URL field (may be empty).
        start:      ``"YYYY-MM-DD"`` for all-day events; ISO-8601 datetime otherwise.
        end:        Same format as ``start``.
        all_day:    ``True`` when the event spans whole days.
        tz_id:      IANA timezone name (e.g. ``"Europe/Moscow"``).
        status:     One of ``"confirmed"``, ``"tentative"``, or ``"cancelled"``.
    """

    state_key: str
    uid: str
    title: str
    location: str
    url: str
    start: str
    end: str
    all_day: bool
    tz_id: str
    status: Literal["confirmed", "tentative", "cancelled"]

    def __repr__(self) -> str:  # noqa: D105
        return f"EventData({self.title!r}, {self.start})"


@dataclass
class SyncStats:
    """Counters and diagnostics collected during a single sync run.

    Attributes:
        created:  Events created in Google.
        linked:   Existing Google events linked to Apple events via content match
                  (no API write performed).
        updated:  Google events updated because a field changed.
        deleted:  Google events deleted because their Apple counterpart was removed.
        skipped:  Apple events already present in Google with no changes.
        missing:  Apple events that could not be confirmed in Google after the
                  verify pass (non-empty indicates a problem).
    """

    created: int = 0
    linked: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    missing: list[EventData] = field(default_factory=list)

    def summary(self) -> str:
        """Return a one-line human-readable summary of the stats."""
        return (
            f"created={self.created}  linked={self.linked}  updated={self.updated}  "
            f"deleted={self.deleted}  skipped={self.skipped}  "
            f"unverified={len(self.missing)}"
        )


@dataclass(frozen=True)
class SyncConfig:
    """All runtime configuration for the sync process.

    Attributes:
        exchange_calendar_name: Calendar name exactly as shown in Apple Calendar.
        google_calendar_id:     Target Google Calendar ID (``"primary"`` or full ID).
        credentials_path:       Path to OAuth 2.0 credentials JSON from Google Cloud.
        token_path:             Path for caching the OAuth access/refresh token.
        state_path:             Path for the JSON state file.
        horizon_past_days:      How many days into the past to include in the window.
        horizon_future_days:    How many days into the future to include.
        verify_wait_secs:       Seconds to wait before the post-sync verification fetch.
        time_tolerance_secs:    Allowed delta (seconds) when comparing event times.
    """

    exchange_calendar_name: str
    google_calendar_id: str
    credentials_path: Path
    token_path: Path
    state_path: Path
    horizon_past_days: int = 7
    horizon_future_days: int = 28
    verify_wait_secs: float = 3.0
    time_tolerance_secs: float = 120.0


# ── Apple Calendar reader ─────────────────────────────────────────────────────


class AppleCalendarReader:
    """Reads calendar events from Apple Calendar via the EventKit framework.

    Requires the ``pyobjc-framework-EventKit`` package and calendar access
    granted in System Settings → Privacy & Security → Calendars.
    """

    _STATUS_TENTATIVE: ClassVar[int] = 2
    _STATUS_CANCELLED: ClassVar[int] = 3

    def __init__(self, calendar_name: str) -> None:
        """Initialise the reader and request macOS calendar access.

        Blocks synchronously until the user responds to the permission dialog.
        On macOS 14+ uses ``requestFullAccessToEventsWithCompletion_``; falls
        back to the older ``requestAccessToEntityType_completion_`` on earlier
        versions.

        Args:
            calendar_name: Name of the calendar as shown in the Apple Calendar app.

        Raises:
            CalendarAccessDeniedError: If macOS calendar access is denied.
            CalendarNotFoundError: If no calendar with ``calendar_name`` exists.
        """
        self._store: Any = self._authorize()
        self._calendar: Any = self._find_calendar(calendar_name)

    # ── Initialisation ────────────────────────────────────────────────────────

    @staticmethod
    def _authorize() -> Any:
        """Request EventKit access and return an authorised ``EKEventStore``.

        Raises:
            CalendarAccessDeniedError: If the user denies access.
        """
        store: Any = EventKit.EKEventStore.alloc().init()
        granted_box: list[bool] = [False]
        sem = threading.Semaphore(0)

        def _cb(granted: bool, _error: Any) -> None:
            granted_box[0] = bool(granted)
            sem.release()

        try:
            store.requestFullAccessToEventsWithCompletion_(_cb)
        except AttributeError:
            store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, _cb)

        sem.acquire()
        if not granted_box[0]:
            raise CalendarAccessDeniedError(
                "Apple Calendar access denied. "
                "Enable it in System Settings → Privacy & Security → Calendars."
            )
        return store

    def _find_calendar(self, name: str) -> Any:
        """Locate the ``EKCalendar`` with the given title.

        Args:
            name: Exact calendar title to look up.

        Raises:
            CalendarNotFoundError: If no calendar matches ``name``.
        """
        calendars: Any = self._store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
        for cal in calendars:
            if str(cal.title()) == name:
                return cal
        available = sorted(str(c.title()) for c in calendars)
        raise CalendarNotFoundError(
            f"Calendar {name!r} not found in Apple Calendar. "
            f"Available: {', '.join(available)}"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_events(self, start: datetime, end: datetime) -> list[EventData]:
        """Return all events in the half-open interval ``[start, end)``.

        Recurring events produce one ``EventData`` per visible occurrence.
        Each occurrence is keyed by ``uid|start`` so it is uniquely addressable
        in the state file even when the base UID is shared.

        Args:
            start: Window start (inclusive), must be timezone-aware.
            end:   Window end (exclusive), must be timezone-aware.

        Returns:
            Deduplicated list of ``EventData`` instances ordered by EventKit.
        """
        ns_start = NSDate.dateWithTimeIntervalSince1970_(start.timestamp())
        ns_end = NSDate.dateWithTimeIntervalSince1970_(end.timestamp())
        pred = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            ns_start, ns_end, [self._calendar]
        )
        raw: Any = self._store.eventsMatchingPredicate_(pred) or []

        seen: set[str] = set()
        result: list[EventData] = []

        for ev in raw:
            uid = str(
                ev.calendarItemExternalIdentifier() or ev.calendarItemIdentifier()
            )
            is_all_day = bool(ev.isAllDay())
            tz_name = str(ev.timeZone().name()) if ev.timeZone() else "UTC"
            tz = self._safe_tz(tz_name)

            ts_start = float(ev.startDate().timeIntervalSince1970())
            ts_end = float(ev.endDate().timeIntervalSince1970())
            start_val = (
                self._ts_to_dt(ts_start, tz).strftime("%Y-%m-%d")
                if is_all_day
                else self._ts_to_dt(ts_start, tz).isoformat()
            )
            end_val = (
                self._ts_to_dt(ts_end, tz).strftime("%Y-%m-%d")
                if is_all_day
                else self._ts_to_dt(ts_end, tz).isoformat()
            )

            state_key = f"{uid}|{start_val}"
            if state_key in seen:
                log.debug("Duplicate state key skipped: %s", state_key)
                continue
            seen.add(state_key)

            status_code = int(ev.status())
            if status_code == self._STATUS_CANCELLED:
                status: Literal["confirmed", "tentative", "cancelled"] = "cancelled"
            elif status_code == self._STATUS_TENTATIVE:
                status = "tentative"
            else:
                status = "confirmed"

            ns_url = ev.URL()
            url = str(ns_url.absoluteString()) if ns_url else ""
            loc = ev.location()
            title = ev.title()

            result.append(
                EventData(
                    state_key=state_key,
                    uid=uid,
                    title=str(title) if title else "",
                    location=str(loc) if loc else "",
                    url=url,
                    start=start_val,
                    end=end_val,
                    all_day=is_all_day,
                    tz_id=tz_name,
                    status=status,
                )
            )

        return result

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _safe_tz(name: str) -> timezone | ZoneInfo:
        """Return a timezone from its IANA name; fall back to UTC on unknown names.

        Args:
            name: IANA timezone string (e.g. ``"Europe/Moscow"``).

        Returns:
            A valid ``ZoneInfo`` or ``timezone.utc``.
        """
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, KeyError):
            return timezone.utc

    @staticmethod
    def _ts_to_dt(timestamp: float, tz: timezone | ZoneInfo) -> datetime:
        """Convert a UNIX timestamp to a timezone-aware datetime.

        Args:
            timestamp: Seconds since the UNIX epoch.
            tz:        Target timezone.

        Returns:
            Timezone-aware ``datetime``.
        """
        return datetime.fromtimestamp(timestamp, tz=tz)


# ── Google Calendar client ────────────────────────────────────────────────────


class GoogleCalendarClient:
    """Thin wrapper around the Google Calendar REST API.

    Handles OAuth token management, pagination, and transient-error retries.
    """

    _SCOPES: ClassVar[list[str]] = ["https://www.googleapis.com/auth/calendar"]
    _MAX_RESULTS_PER_PAGE: ClassVar[int] = 2500
    _RETRYABLE_STATUSES: ClassVar[frozenset[int]] = frozenset({429, 500, 503})
    _RETRY_DELAY_SECS: ClassVar[float] = 5.0

    def __init__(
        self,
        calendar_id: str,
        credentials_path: Path,
        token_path: Path,
    ) -> None:
        """Initialise the client, refreshing or acquiring OAuth credentials.

        On the first run (no token file) a browser window opens for interactive
        authorisation.  Subsequent runs refresh the stored token automatically.

        Args:
            calendar_id:      Google Calendar ID.  Use ``"primary"`` for the main
                              calendar or the full address-style ID from Calendar
                              Settings.
            credentials_path: Path to ``credentials.json`` downloaded from Google
                              Cloud Console (OAuth 2.0 client secret).
            token_path:       Where to cache the access/refresh token between runs.

        Raises:
            FileNotFoundError: If ``credentials_path`` does not exist.
        """
        self._calendar_id = calendar_id
        self._service: Any = self._build_service(credentials_path, token_path)

    def _build_service(self, credentials_path: Path, token_path: Path) -> Any:
        """Authenticate and return a built ``googleapiclient`` service resource.

        Args:
            credentials_path: Path to OAuth client-secret JSON.
            token_path:       Path where the token will be cached.

        Returns:
            Authorised Google Calendar service resource.

        Raises:
            FileNotFoundError: If ``credentials_path`` is missing.
        """
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"Google credentials file not found: {credentials_path}\n"
                "Download it from Google Cloud Console → APIs & Services → Credentials."
            )

        creds: Credentials | None = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), self._SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                log.info("Refreshing Google OAuth token…")
                creds.refresh(Request())
            else:
                log.info("Starting interactive OAuth flow (browser will open)…")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path), self._SCOPES
                )
                creds = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json())
            log.info("Token cached at %s", token_path)

        return build("calendar", "v3", credentials=creds)

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_events(self, start: datetime, end: datetime) -> list[GoogleEvent]:
        """Fetch all non-deleted events in the given window, handling pagination.

        Args:
            start: Window start (inclusive), must be timezone-aware.
            end:   Window end (exclusive), must be timezone-aware.

        Returns:
            List of raw Google Calendar event resource dicts.
        """
        result: list[GoogleEvent] = []
        page_token: str | None = None

        while True:
            resp: dict[str, Any] = (
                self._service.events()
                .list(
                    calendarId=self._calendar_id,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    maxResults=self._MAX_RESULTS_PER_PAGE,
                    pageToken=page_token,
                    showDeleted=False,
                )
                .execute()
            )
            result.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return result

    def create_event(self, ev: EventData) -> str:
        """Create a new event and return its Google event ID.

        Retries once on transient server errors (429, 500, 503).

        Args:
            ev: Event data to create.

        Returns:
            Google event ID of the newly created event.

        Raises:
            HttpError: On a non-retryable API error.
        """
        body = self._build_body(ev)
        last_exc: HttpError | None = None

        for attempt in range(2):
            try:
                created: GoogleEvent = (
                    self._service.events()
                    .insert(calendarId=self._calendar_id, body=body)
                    .execute()
                )
                return created["id"]
            except HttpError as exc:
                last_exc = exc
                if exc.resp.status in self._RETRYABLE_STATUSES and attempt == 0:
                    log.warning(
                        "HTTP %s on insert — retrying in %.0f s…",
                        exc.resp.status,
                        self._RETRY_DELAY_SECS,
                    )
                    time.sleep(self._RETRY_DELAY_SECS)
                else:
                    raise

        raise last_exc  # type: ignore[misc]

    def update_event(self, google_id: str, ev: EventData) -> None:
        """Update an existing event.

        Args:
            google_id: ID of the Google event to update.
            ev:        New event data to write.

        Raises:
            HttpError: On an API error.
        """
        body = self._build_body(ev)
        self._service.events().update(
            calendarId=self._calendar_id,
            eventId=google_id,
            body=body,
        ).execute()

    def delete_event(self, google_id: str) -> None:
        """Delete a Google Calendar event.

        Args:
            google_id: ID of the event to delete.

        Raises:
            HttpError: On a non-404/410 API error.
        """
        self._service.events().delete(
            calendarId=self._calendar_id,
            eventId=google_id,
        ).execute()

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _build_body(ev: EventData) -> dict[str, Any]:
        """Convert an ``EventData`` into a Google Calendar API request body.

        The meeting URL (if present) is written to the ``description`` field so
        it survives round-trips and is searchable by the content matcher.

        Args:
            ev: Source event data.

        Returns:
            Dict suitable for use as a Google Calendar API request body.
        """
        body: dict[str, Any] = {"summary": ev.title, "status": ev.status}

        if ev.location:
            body["location"] = ev.location
        if ev.url:
            body["description"] = ev.url

        if ev.all_day:
            body["start"] = {"date": ev.start}
            body["end"] = {"date": ev.end}
        else:
            body["start"] = {"dateTime": ev.start, "timeZone": ev.tz_id}
            body["end"] = {"dateTime": ev.end, "timeZone": ev.tz_id}

        return body


# ── Sync state ────────────────────────────────────────────────────────────────


class SyncState:
    """Persists the ``state_key → google_event_id`` mapping between sync runs.

    The state key has the form ``{apple_uid}|{start_value}``.  This encoding
    makes each recurring-event occurrence independently addressable even though
    all occurrences share the same ``calendarItemExternalIdentifier``.
    """

    def __init__(self, path: Path) -> None:
        """Load (or initialise) state from disk.

        Args:
            path: Path to the JSON state file.  The file and any missing parent
                  directories are created automatically on the first ``save()``.
        """
        self._path = path
        self._data: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        """Read the state file from disk.

        Returns:
            Parsed mapping, or an empty dict if the file is absent or corrupt.
        """
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read state file (%s) — starting fresh.", exc)
        return {}

    def save(self, mapping: dict[str, str]) -> None:
        """Write the mapping to disk and update the in-memory copy.

        Args:
            mapping: Updated ``state_key → google_event_id`` dict.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(mapping, indent=2, ensure_ascii=False))
        self._data = mapping

    @property
    def data(self) -> dict[str, str]:
        """Return a shallow copy of the current in-memory state."""
        return dict(self._data)


# ── Syncer ────────────────────────────────────────────────────────────────────


class CalendarSyncer:
    """Orchestrates one-way sync from Apple Calendar to Google Calendar.

    The sync is structured as three sequential passes per run:

    1. **Upsert pass** — for every Apple event in the window, find the
       corresponding Google event (via state lookup or content match) and create
       or update it as needed.
    2. **Delete pass** — for every state entry whose Apple event has disappeared,
       delete the corresponding Google event if it is still within the window.
    3. **Verify pass** — re-fetch Google Calendar and confirm every Apple event
       is now visible; unresolved events are collected in ``SyncStats.missing``.
    """

    def __init__(
        self,
        apple: AppleCalendarReader,
        google: GoogleCalendarClient,
        state: SyncState,
        verify_wait_secs: float = 3.0,
        time_tolerance_secs: float = 120.0,
    ) -> None:
        """Create a ``CalendarSyncer``.

        Args:
            apple:               Configured Apple Calendar reader.
            google:              Configured Google Calendar client.
            state:               Persistent state manager.
            verify_wait_secs:    Seconds to wait before the verification re-fetch
                                 (gives Google time to index newly created events).
            time_tolerance_secs: Maximum allowed start/end delta (seconds) before
                                 an update is triggered.
        """
        self._apple = apple
        self._google = google
        self._state = state
        self._verify_wait = verify_wait_secs
        self._tolerance = time_tolerance_secs

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, start: datetime, end: datetime) -> SyncStats:
        """Execute a full sync cycle for the given time window.

        Args:
            start: Sync window start (inclusive), must be timezone-aware.
            end:   Sync window end (exclusive), must be timezone-aware.

        Returns:
            ``SyncStats`` with per-operation counts and any unresolved events.
        """
        apple_events = self._apple.fetch_events(start, end)
        google_events = self._google.fetch_events(start, end)
        google_by_id: dict[str, GoogleEvent] = {ev["id"]: ev for ev in google_events}
        state_data = self._state.data

        log.info("Apple events fetched : %d", len(apple_events))
        log.info("Google events fetched: %d", len(google_by_id))
        log.info("State entries loaded : %d", len(state_data))

        new_state, stats = self._upsert_pass(apple_events, google_by_id, state_data)
        self._delete_pass(state_data, google_by_id, new_state, stats)
        stats.missing = self._verify_pass(apple_events, new_state, start, end)

        self._state.save(new_state)
        return stats

    # ── Sync passes ───────────────────────────────────────────────────────────

    def _upsert_pass(
        self,
        apple_events: list[EventData],
        google_by_id: dict[str, GoogleEvent],
        state_data: dict[str, str],
    ) -> tuple[dict[str, str], SyncStats]:
        """Ensure every Apple event has a corresponding Google event.

        For each Apple event the method attempts to resolve its Google counterpart
        in this order:

        1. State lookup by ``state_key``.
        2. Content match (title + time + URL) against all current Google events.
        3. Create a new Google event if no match is found.

        Newly created events are appended to an internal search list so that
        duplicate Apple entries (an edge case) do not produce a second copy.

        Args:
            apple_events: Events fetched from Apple Calendar.
            google_by_id: Current Google events indexed by Google event ID.
            state_data:   Previous-run state mapping.

        Returns:
            A tuple of ``(new_state, stats)`` where ``new_state`` contains all
            resolved ``state_key → google_id`` pairs.
        """
        new_state: dict[str, str] = {}
        stats = SyncStats()
        # Extended as events are created so the content matcher stays up-to-date
        # within the same pass.
        google_list: list[GoogleEvent] = list(google_by_id.values())

        for ev in apple_events:
            stored_id = state_data.get(ev.state_key)
            google_ev: GoogleEvent | None = (
                google_by_id.get(stored_id) if stored_id else None
            )

            if google_ev is None:
                google_ev = self._find_google_match(ev, google_list)

            if google_ev is not None:
                new_state[ev.state_key] = google_ev["id"]
                if self._events_differ(ev, google_ev):
                    try:
                        self._google.update_event(google_ev["id"], ev)
                        stats.updated += 1
                        log.info("Updated : %s", ev)
                    except HttpError as exc:
                        log.error("Failed to update %s: %s", ev, exc)
                elif stored_id != google_ev["id"]:
                    stats.linked += 1
                    log.info("Linked  : %s", ev)
                else:
                    stats.skipped += 1
            else:
                try:
                    new_id = self._google.create_event(ev)
                    new_state[ev.state_key] = new_id
                    # Register the stub so duplicates within this pass are caught.
                    stub: GoogleEvent = {  # type: ignore[typeddict-unknown-key]
                        "id": new_id,
                        "summary": ev.title,
                        "description": ev.url,
                        "start": {"date": ev.start} if ev.all_day else {"dateTime": ev.start},
                        "end": {"date": ev.end} if ev.all_day else {"dateTime": ev.end},
                    }
                    google_list.append(stub)
                    stats.created += 1
                    log.info("Created : %s", ev)
                except HttpError as exc:
                    log.error("Failed to create %s: %s", ev, exc)

        return new_state, stats

    def _delete_pass(
        self,
        state_data: dict[str, str],
        google_by_id: dict[str, GoogleEvent],
        new_state: dict[str, str],
        stats: SyncStats,
    ) -> None:
        """Remove Google events whose Apple counterparts have disappeared.

        Only events *within the current sync window* (i.e. present in
        ``google_by_id``) are eligible for deletion to avoid touching events
        outside the horizon.  Failed deletes are re-inserted into ``new_state``
        so they are retried on the next run.

        Args:
            state_data:   Previous-run state mapping.
            google_by_id: Current Google events indexed by Google event ID.
            new_state:    The state dict being built; may be extended on failure.
            stats:        Mutable stats object; ``deleted`` counter is incremented.
        """
        for key, google_id in state_data.items():
            if key in new_state:
                continue
            google_ev = google_by_id.get(google_id)
            if google_ev is None:
                continue  # outside window or already gone
            try:
                self._google.delete_event(google_id)
                stats.deleted += 1
                log.info("Deleted : %s", google_ev.get("summary", google_id))
            except HttpError as exc:
                if exc.resp.status in (404, 410):
                    pass  # already gone — do not re-add to state
                else:
                    log.error("Failed to delete %s: %s", google_id, exc)
                    new_state[key] = google_id  # retry on next run

    def _verify_pass(
        self,
        apple_events: list[EventData],
        new_state: dict[str, str],
        start: datetime,
        end: datetime,
    ) -> list[EventData]:
        """Re-fetch Google and confirm every Apple event is present.

        Waits ``verify_wait_secs`` before fetching to give Google time to index
        newly created events.  Each Apple event is confirmed either by its stored
        Google ID or by a content match against the fresh snapshot.

        Args:
            apple_events: Full list of Apple events that should exist in Google.
            new_state:    State produced by the upsert pass.
            start:        Sync window start (re-used for the verification fetch).
            end:          Sync window end.

        Returns:
            List of Apple events that could *not* be confirmed in Google.
        """
        log.info(
            "Verification: waiting %.0f s then re-fetching Google…", self._verify_wait
        )
        time.sleep(self._verify_wait)

        fresh_events = self._google.fetch_events(start, end)
        fresh_by_id: dict[str, GoogleEvent] = {ev["id"]: ev for ev in fresh_events}
        fresh_list = list(fresh_events)

        missing: list[EventData] = []
        for ev in apple_events:
            google_id = new_state.get(ev.state_key)
            if google_id and google_id in fresh_by_id:
                continue
            if self._find_google_match(ev, fresh_list) is not None:
                continue
            missing.append(ev)

        return missing

    # ── Matching helpers ──────────────────────────────────────────────────────

    def _find_google_match(
        self,
        ev: EventData,
        candidates: list[GoogleEvent],
    ) -> GoogleEvent | None:
        """Find a Google event corresponding to the given Apple event.

        Matching requires **all** of the following to hold:

        * **Title** equality (case-insensitive, stripped).
        * **Start and end** times within ``time_tolerance_secs``.
        * **URL** — if the Apple event carries a URL, that URL must appear
          somewhere in the Google event (description, location, hangoutLink,
          or a conferenceData entry point).

        When a URL is present and multiple title+time candidates exist, only
        those containing the URL are returned.  When exactly one candidate
        matches on title+time but is missing the URL, it is linked anyway — the
        URL may simply not have been written on a previous partial sync.

        Args:
            ev:         Apple event to look up.
            candidates: Google events to search through.

        Returns:
            Best-matching ``GoogleEvent`` dict, or ``None`` if no match is found.
        """
        apple_title = ev.title.strip().lower()

        time_matches = [
            g for g in candidates
            if (g.get("summary") or "").strip().lower() == apple_title
            and self._times_match(ev, g)
        ]

        if not time_matches:
            return None

        if not ev.url:
            return time_matches[0]

        url_matches = [g for g in time_matches if ev.url in self._url_haystack(g)]
        if url_matches:
            return url_matches[0]

        # URL not found: link unambiguously, otherwise create a new event.
        return time_matches[0] if len(time_matches) == 1 else None

    def _events_differ(self, ev: EventData, gev: GoogleEvent) -> bool:
        """Return ``True`` if any synced field has changed and an update is needed.

        Fields compared: title, location, URL, status, start, end.

        Args:
            ev:  Authoritative Apple event.
            gev: Current Google event state.

        Returns:
            ``True`` when at least one field differs beyond the allowed tolerance.
        """
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
        """Return ``True`` if event start/end times agree within tolerance.

        For all-day events an exact date-string match is required.  For timed
        events both boundaries must be within ``time_tolerance_secs``.

        Args:
            ev:  Apple event.
            gev: Google event.

        Returns:
            ``True`` when times are considered equivalent.
        """
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

    # ── Static utilities ──────────────────────────────────────────────────────

    @staticmethod
    def _url_haystack(gev: GoogleEvent) -> str:
        """Concatenate all URL-bearing fields of a Google event for substring search.

        Includes ``description``, ``location``, ``hangoutLink``, and all
        ``conferenceData.entryPoints[].uri`` values.

        Args:
            gev: Google event resource dict.

        Returns:
            Single newline-separated string of all URL content.
        """
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
            s: Datetime string to parse (empty string is handled gracefully).

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


# ── Entry point ───────────────────────────────────────────────────────────────


def _configure_logging() -> None:
    """Configure root logger to write INFO+ to stdout."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def main() -> None:
    """CLI entry point.

    Reads ``config.py``, builds all components, runs a sync cycle, and exits
    with a non-zero code when any event could not be verified in Google.

    Exit codes:
        0:   Sync completed and all events verified.
        1:   Configuration or authentication error.
        2:   Sync completed but one or more events could not be verified.
        130: Interrupted by the user (Ctrl-C).
    """
    _configure_logging()

    cfg = SyncConfig(
        exchange_calendar_name=config.EXCHANGE_CALENDAR_NAME,
        google_calendar_id=config.GOOGLE_CALENDAR_ID,
        credentials_path=Path(config.CREDENTIALS_PATH).expanduser(),
        token_path=Path(config.TOKEN_PATH).expanduser(),
        state_path=Path(config.STATE_PATH).expanduser(),
    )

    now = datetime.now(tz=timezone.utc)
    sync_start = now - timedelta(days=cfg.horizon_past_days)
    sync_end = now + timedelta(days=cfg.horizon_future_days)

    log.info(
        "=== Calendar sync start  [%s → %s] ===",
        sync_start.date(),
        sync_end.date(),
    )
    log.info("Source : Apple Calendar / %r", cfg.exchange_calendar_name)
    log.info("Target : Google Calendar / %r", cfg.google_calendar_id)

    try:
        apple = AppleCalendarReader(cfg.exchange_calendar_name)
        google = GoogleCalendarClient(
            cfg.google_calendar_id, cfg.credentials_path, cfg.token_path
        )
        state = SyncState(cfg.state_path)
        syncer = CalendarSyncer(
            apple,
            google,
            state,
            verify_wait_secs=cfg.verify_wait_secs,
            time_tolerance_secs=cfg.time_tolerance_secs,
        )

        stats = syncer.run(sync_start, sync_end)

        log.info("=== Sync complete: %s ===", stats.summary())

        if stats.missing:
            log.warning(
                "%d event(s) could not be verified in Google:", len(stats.missing)
            )
            for ev in stats.missing:
                log.warning("  MISSING  %s  [%s – %s]", ev.title, ev.start, ev.end)
            sys.exit(2)

    except CalendarAccessDeniedError as exc:
        log.error("%s", exc)
        sys.exit(1)
    except CalendarNotFoundError as exc:
        log.error("%s", exc)
        sys.exit(1)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()

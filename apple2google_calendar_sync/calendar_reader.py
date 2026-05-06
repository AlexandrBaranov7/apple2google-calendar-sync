"""Calendar reader abstraction and Apple Calendar implementation."""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import objc  # noqa: F401
from Foundation import NSDate
import EventKit

from .exceptions import CalendarAccessDeniedError, CalendarNotFoundError
from .models import EventData

log = logging.getLogger(__name__)


class CalendarReader(ABC):
    """Abstract interface for reading calendar events."""

    @abstractmethod
    def fetch_events(self, start: datetime, end: datetime) -> list[EventData]:
        """Fetch all events in the given time window.

        Args:
            start: Window start (inclusive), must be timezone-aware.
            end:   Window end (exclusive), must be timezone-aware.

        Returns:
            List of ``EventData`` instances for all matching events.
        """


class AppleCalendarReader(CalendarReader):
    """Reads calendar events from Apple Calendar via the EventKit framework.

    Requires the ``pyobjc-framework-EventKit`` package and calendar access
    granted in System Settings → Privacy & Security → Calendars.
    """

    _STATUS_TENTATIVE: ClassVar[int] = 2
    _STATUS_CANCELLED: ClassVar[int] = 3

    def __init__(self, calendar_name: str) -> None:
        """Initialise the reader and request macOS calendar access.

        Blocks synchronously until the user responds to the permission dialog.

        Args:
            calendar_name: Name of the calendar as shown in the Apple Calendar app.

        Raises:
            CalendarAccessDeniedError: If macOS calendar access is denied.
            CalendarNotFoundError: If no calendar with ``calendar_name`` exists.
        """
        self._store: Any = self._authorize()
        self._calendar: Any = self._find_calendar(calendar_name)

    @staticmethod
    def _authorize() -> Any:
        """Request EventKit access and return an authorised ``EKEventStore``."""
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

    def fetch_events(self, start: datetime, end: datetime) -> list[EventData]:
        """Return all events in the half-open interval ``[start, end)``.

        Recurring events produce one ``EventData`` per visible occurrence.

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
                status: Any = "cancelled"
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

    @staticmethod
    def _safe_tz(name: str) -> Any:
        """Return a timezone from its IANA name; fall back to UTC on unknown names."""
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, KeyError):
            from datetime import timezone
            return timezone.utc

    @staticmethod
    def _ts_to_dt(timestamp: float, tz: Any) -> datetime:
        """Convert a UNIX timestamp to a timezone-aware datetime."""
        return datetime.fromtimestamp(timestamp, tz=tz)

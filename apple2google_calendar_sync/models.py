"""Data models: events, sync statistics, and configuration."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class EventData:
    """Immutable snapshot of a single calendar event instance.

    Attributes:
        state_key:  Unique key used in the state file, formed as ``uid|start``.
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
        linked:   Existing Google events linked to Apple events via content match.
        updated:  Google events updated because a field changed.
        deleted:  Google events deleted because their Apple counterpart was removed.
        skipped:  Apple events already present in Google with no changes.
        missing:  Apple events that could not be confirmed in Google after verify.
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

"""Type definitions for Google Calendar API responses."""
from typing import Any, TypedDict


class _TimeSlot(TypedDict, total=False):
    """Start or end time in a Google Calendar event."""

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

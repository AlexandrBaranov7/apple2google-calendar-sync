"""Custom exception types for the calendar sync module."""


class CalendarSyncError(Exception):
    """Base exception for all calendar sync errors."""


class CalendarAccessDeniedError(CalendarSyncError):
    """Apple Calendar access was denied in System Settings."""


class CalendarNotFoundError(CalendarSyncError):
    """The requested calendar name does not exist in Apple Calendar."""

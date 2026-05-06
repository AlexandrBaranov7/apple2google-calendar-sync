"""Enables ``python -m apple2google_calendar_sync``."""
import sys

from .cli import main

sys.exit(main())

"""
backend/utils.py
Shared utility functions used across multiple backend modules.
"""
from datetime import date


def today() -> str:
    """Return today's date as ISO string (YYYY-MM-DD)."""
    return str(date.today())

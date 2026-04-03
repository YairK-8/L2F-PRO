"""
backend/utils.py
Shared utility functions used across multiple backend modules.
"""
from datetime import date


def today() -> str:
    """Return today's date as ISO string (YYYY-MM-DD)."""
    return str(date.today())


def normalize_barcode(value: str) -> str:
    """
    Normalize scanner/input barcode so E123 and 123 are treated as the same.
    - Strip whitespace
    - Uppercase
    - Remove leading 'E' if present
    """
    v = str(value or "").strip().upper()
    if v.startswith("E"):
        v = v[1:]
    return v

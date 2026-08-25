"""Upstream mcp_convert database exports; CSV backend is optional for LOCA.

The Google Cloud local MCP uses JsonDatabase and SQLiteBackend only. Keeping the
optional import lazy preserves the pinned service behavior without requiring the
unrelated pandas dependency in the Harbor image.
"""
from .base import BaseDatabase
from .json_db import JsonDatabase
try:
    from .csv_db import CsvDatabase
except ModuleNotFoundError:
    CsvDatabase = None
__all__ = ["BaseDatabase", "JsonDatabase", "CsvDatabase"]

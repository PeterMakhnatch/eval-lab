"""Hackbook: versioned catalog of known ways agents cheat and tasks break."""

from evallab.hackbook.matcher import match_target, match_task, match_trace
from evallab.hackbook.models import (
    STRUCTURAL_SIGNALS,
    HackbookEntry,
    get_entry,
    list_entries,
    load_hackbook,
    load_taxonomy,
    search_entries,
    validate_no_tb4_test_leak,
)

__all__ = [
    "STRUCTURAL_SIGNALS",
    "HackbookEntry",
    "get_entry",
    "list_entries",
    "load_hackbook",
    "load_taxonomy",
    "match_target",
    "match_task",
    "match_trace",
    "search_entries",
    "validate_no_tb4_test_leak",
]

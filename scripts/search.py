#!/usr/bin/env python3
"""Search helper for the file manager list (imported by scripts/list.py). The needle travels in the content URL's q param; Skin.Get/SetSkinString do NOT exist in this build's JSON-RPC, so the list reload uses Container.Refresh via main.py's "search" command.
"""
from urllib.parse import parse_qs, urlparse

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import log as _common_log



def log(msg):
    # Sanitized sink (see list.py): the URL may carry undecodable names.
    _common_log("search: " + msg)


def needle_from_url(url):
    """Lowercased search needle from the q param; "" = unfiltered. Callers must pass the FULL url: Kodi strips the query from argv[0], it arrives as argv[2]."""
    try:
        vals = parse_qs(urlparse(url).query).get("q", [])
        needle = vals[0].strip().lower() if vals else ""
    except Exception:
        needle = ""
    log("needle=%r from %s" % (needle, url))
    return needle


def hidden_from_url(url):
    """Show-hidden-files flag from the h param (mirrors Skin.Bool(show.hidden): non-empty when on, EMPTY when off, so a non-empty check is locale-proof). Same reload-trigger dual-use as q."""
    try:
        vals = parse_qs(urlparse(url).query).get("h", [])
        on = bool(vals and vals[0].strip())
    except Exception:
        on = False
    log("show_hidden=%s from %s" % (on, url))
    return on


def sort_from_url(url):
    """Sort mode from the sort param (name/size/date); defaults to name if missing/invalid."""
    try:
        vals = parse_qs(urlparse(url).query).get("sort", [])
        mode = vals[0].strip().lower() if vals else "name"
        if mode not in ("name", "size", "date"):
            mode = "name"
    except Exception:
        mode = "name"
    log("sort=%r from %s" % (mode, url))
    return mode


def foldersfirst_from_url(url):
    """Folders-first flag from the ff param (opt-out folders.last): the flag is INVERTED here (empty ff = folders first, the default). Toggling still flips the URL value, keeping the live reload trigger."""
    try:
        vals = parse_qs(urlparse(url).query).get("ff", [])
        on = not bool(vals and vals[0].strip())
    except Exception:
        on = True
    log("folders_first=%s from %s" % (on, url))
    return on


def casesensitive_from_url(url):
    """Case-sensitive flag from the cs param: Layout.xml mirrors Skin.HasSetting(blacklist.casesensitive), so any non-empty value means true (also the reload trigger)."""
    try:
        vals = parse_qs(urlparse(url).query).get("cs", [])
        on = bool(vals and vals[0].strip())
    except Exception:
        on = False
    log("case_sensitive=%s from %s" % (on, url))
    return on

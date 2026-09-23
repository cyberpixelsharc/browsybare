#!/usr/bin/env python3
"""User directory sources for Browsybare (directory sources). Managed in the settings rubric (bp.dirsrc rows); runtime file userdata/skindev/directory-sources.json, created on first change, no seed (starts empty)."""
import json

import xbmc

from common import read_text, write_text, state_file


def _runtime_path():
    return state_file("directory-sources.json")


def load():
    """Stored sources (stripped strings, order preserved) or []."""
    txt = read_text(_runtime_path())
    if txt:
        try:
            data = json.loads(txt)
            if isinstance(data, list):
                return [s for s in data if isinstance(s, str) and s.strip()]
        except Exception:
            pass
    return []


def save(sources):
    return write_text(_runtime_path(), json.dumps(sources, ensure_ascii=False, indent=2) + "\n")


def add(source):
    """Append a source; trailing slashes go, except after the scheme. New sources are auto-enabled: clears the path-hash hide key (hide.dirsource.<hash>)."""
    s = (source or "").strip()
    while s.endswith("/") and not s.endswith("://"):
        s = s[:-1]
    if not s:
        return False
    cur = load()
    if s in cur:
        return False
    cur.append(s)
    save(cur)
    try:
        import sources as _sources
        xbmc.executebuiltin(
            "Skin.Reset(hide.dirsource.%s)" % _sources.dirsource_key(s))
    except Exception:
        pass
    return True


def remove(idx):
    """Remove the 1-based entry (and clear its hide key so a later re-add is not hidden)."""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return False
    cur = load()
    if 1 <= idx <= len(cur):
        removed = cur[idx - 1]
        del cur[idx - 1]
        save(cur)
        try:
            import sources as _sources
            xbmc.executebuiltin(
                "Skin.Reset(hide.dirsource.%s)" % _sources.dirsource_key(removed))
        except Exception:
            pass
        return True
    return False

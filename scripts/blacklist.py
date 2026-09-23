#!/usr/bin/env python3
"""User blacklist management for Browsybare (blocked file/folder patterns). EFFECTIVE list = baseline from data/blacklist.json (never removable) + user patterns from userdata/skindev/blacklist.json; matching is case-sensitive only when the blacklist.casesensitive setting is on."""
import json
import os

from common import read_text, write_text, state_dir, state_file, skin_root, safe_label, sort_fold


def _runtime_path():
    return os.path.join(state_dir(), "blacklist.json")


def _seed_path():
    return os.path.join(skin_root(), "data", "blacklist.json")


def _read_list(path):
    txt = read_text(path)
    data = []
    if txt:
        try:
            data = json.loads(txt)
        except Exception:
            data = []
    return data if isinstance(data, list) else []


def _normalize(patterns):
    """Stripped, deduplicated (case-sensitive), order preserved."""
    seen, out = set(), []
    for p in patterns if isinstance(patterns, list) else []:
        if isinstance(p, str) and p.strip():
            n = p.strip()
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def seed_patterns():
    """Baseline patterns from data/blacklist.json (standard values)."""
    return _normalize(_read_list(_seed_path()))


def user_patterns():
    """User-added patterns from the runtime file; seed entries are ignored case-insensitively (legacy files may carry the old full lowercased copy)."""
    seed_lower = {p.lower() for p in seed_patterns()}
    return [p for p in _normalize(_read_list(_runtime_path()))
            if p.lower() not in seed_lower]


def load():
    """Effective patterns: baseline + user, deduplicated and sorted case-insensitively."""
    out, seen = [], set()
    for p in seed_patterns() + user_patterns():
        key = p.lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return sorted(out, key=str.lower)


def save_user(patterns):
    """Write the USER list (normalized, atomic via write_text)."""
    write_text(_runtime_path(), json.dumps(_normalize(patterns), ensure_ascii=False, indent=1) + "\n")


def _off_path():
    return state_file("blacklist-off.json")


def off_patterns():
    """Patterns toggled OFF in the settings row (runtime off-list)."""
    return _normalize(_read_list(_off_path()))


def load_active():
    """Effective patterns minus the toggled-off ones (what filters in list.py/foldersize.py); off-list comparison is case-insensitive."""
    off = {p.lower() for p in off_patterns()}
    return [p for p in load() if p.lower() not in off]


def blocked(name, patterns, case_sensitive=False):
    """Canonical blacklist matcher (list.py, fileops.py, foldersize.py): exact name, or extension/suffix with a dot separator ("jpg"/".jpg" -> *.jpg). Case-sensitive only when case_sensitive is on; also compares on the folded display form so umlaut patterns match surrogate-escaped names."""
    n = name if case_sensitive else name.lower()
    for p in patterns:
        pp = p if case_sensitive else p.lower()
        if n == pp:
            return True
        if pp.startswith(".") and n.endswith(pp):
            return True
        if not pp.startswith(".") and n.endswith("." + pp):
            return True
    fold = (lambda s: safe_label(s)) if case_sensitive else (lambda s: sort_fold(safe_label(s)))
    fn = fold(name)
    for p in patterns:
        fp = fold(p)
        if fn == fp:
            return True
        if fp.startswith(".") and fn.endswith(fp):
            return True
        if not fp.startswith(".") and fn.endswith("." + fp):
            return True
    return False


def toggle(pattern):
    """Enable/disable a pattern in the off-list; returns True when now OFF."""
    if not pattern or not pattern.strip():
        return False
    pattern = pattern.strip()
    off = off_patterns()
    key = pattern.lower()
    match = next((p for p in off if p.lower() == key), None)
    if match is not None:
        off.remove(match)
    else:
        off.append(pattern)
    write_text(_off_path(), json.dumps(off, ensure_ascii=False, indent=1) + "\n")
    return any(p.lower() == key for p in off)


def add(pattern):
    """Add a user pattern; returns True when the list changed. Membership is checked case-INSENSITIVELY (dedup is case-insensitive)."""
    if not pattern or not pattern.strip():
        return False
    n = pattern.strip()
    if n.lower() in {p.lower() for p in load()}:
        return False
    user = user_patterns()
    user.append(n)
    save_user(user)
    return True


def replace(index, pattern):
    """Replace the 1-based entry of the EFFECTIVE list with a new user pattern; returns True when the list changed. Baseline entries cannot be edited; a duplicate value is rejected; the disabled state carries over."""
    if not pattern or not pattern.strip():
        return False
    n = pattern.strip()
    merged = load()
    if not (1 <= index <= len(merged)):
        return False
    entry = merged[index - 1]
    if entry in set(seed_patterns()):
        return False
    user = user_patterns()
    if entry not in user:
        return False
    if n.lower() in {p.lower() for p in merged if p.lower() != entry.lower()}:
        return False
    if n != entry:
        user[user.index(entry)] = n
        save_user(user)
        off = off_patterns()
        match = next((p for p in off if p.lower() == entry.lower()), None)
        if match is not None:
            off[off.index(match)] = n
            write_text(_off_path(), json.dumps(off, ensure_ascii=False, indent=1) + "\n")
    return True


def remove(index):
    """Remove the 1-based entry of the EFFECTIVE list; returns True when the list changed. Baseline entries cannot be removed."""
    merged = load()
    if not (1 <= index <= len(merged)):
        return False
    entry = merged[index - 1]
    if entry in set(seed_patterns()):
        return False
    user = user_patterns()
    if entry in user:
        user.remove(entry)
        save_user(user)
        # Drop any stale off-list entry: a re-added pattern must not come back disabled.
        off = off_patterns()
        if entry in off:
            off.remove(entry)
            write_text(_off_path(), json.dumps(off, ensure_ascii=False, indent=1) + "\n")
        return True
    return False
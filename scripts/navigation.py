#!/usr/bin/env python3
"""Navigation + breadcrumb state for Browsybare (Home window). set_current() is the single writer of bp.path/bp.src/bp.title/bp.crumb*; bp.path/bp.src are stored PERCENT-ENCODED (window properties mangle surrogateescape names, so every reader decodes via common.path_dec)."""
import os
import time

import xbmc
import xbmcgui

import sources
from common import fs_path, log, safe_label, redact, safe_key, path_enc, path_dec

CRUMB_DEPTH = 7  # max visible breadcrumb segments (buttons 40-46)
CRUMB_SEG_MAX = 17  # per-segment name cap (longer -> first 17 chars + "…")


def reset_top():
    """Park the list cursor on the first content row before a content swap (FirstPage + two downs; index 2 is always the first content row). Only runs while the list already has focus, so focus is never stolen."""
    try:
        if xbmc.getCondVisibility("Control.HasFocus(33)"):
            xbmc.executebuiltin("Action(FirstPage)")
            xbmc.executebuiltin("Action(down)")
            xbmc.executebuiltin("Action(down)")
    except Exception:
        pass


# The list loads <content>$INFO[...bp.path]</content>; changing the property
# reloads it. WindowTranslator knows only window names, so we navigate via bp.path.


def resolve_real(path):
    """Repair a mangled arrival path back to the stored filesystem entry (exact name, sanitized display form, then NFC-normalized). Returns the input unchanged when nothing matches; normally a no-op (paths travel ASCII-encoded via bp.url)."""
    try:
        if path and os.path.isdir(path):
            return path
        parent, base = os.path.split(path or "")
        if not parent or not base or not os.path.isdir(parent):
            return path
        try:
            names = os.listdir(parent)
        except OSError:
            return path
        if base in names:
            return os.path.join(parent, base)
        want = safe_key(base)
        for n in names:
            if safe_key(n) == want:
                log("nav: repaired arrival %s" % redact(path))
                return os.path.join(parent, n)
        import unicodedata
        want_nfc = unicodedata.normalize("NFC", base)
        for n in names:
            if unicodedata.normalize("NFC", n) == want_nfc:
                log("nav: repaired arrival (NFC) %s" % redact(path))
                return os.path.join(parent, n)
        log("nav: unrepairable arrival %s (%d siblings)" % (redact(path), len(names)))
    except Exception:
        pass
    return path


def drive_root_of(path):
    """Physical drive root (macOS: /Volumes/<name>; else home)."""
    if path.startswith("/Volumes/"):
        return "/Volumes/" + path.split("/Volumes/")[1].split("/")[0]
    return os.path.expanduser("~")


def dirsource_root_of(path):
    """Deepest directory source containing `path` (path-based, no state)."""
    try:
        import dirsources as _ds
        best = ""
        for p in _ds.load():
            pp = (p or "").strip().rstrip("/")
            if pp and (path == pp or path.startswith(pp + "/")) and len(pp) > len(best):
                best = pp
        return best
    except Exception:
        return ""


def current_source_root(path):
    """Source root the view is under: the entered source (bp.src) while the path stays inside it, else re-resolved from the path (drive root, then containing dirsource, else home). Browsing into a dirsource from its drive does NOT switch the source."""
    path = sources.rstrip_slash(path or "")
    win = xbmcgui.Window(10000)
    src = sources.rstrip_slash(path_dec(win.getProperty("bp.src") or ""))
    if src and (path == src or path.startswith(src + "/")):
        return src
    if path.startswith("/Volumes/"):
        return drive_root_of(path)
    return dirsource_root_of(path) or os.path.expanduser("~")


def crumb_of(path):
    # Display path relative to the source root, format "/ A / B" ("" at root); the source name lives in the chip.
    root = current_source_root(path)
    if path == root:
        return ""
    if path.startswith(root + "/"):
        rel = path[len(root) + 1:]
    else:
        for prefix in ("/Volumes/", "Volumes\\"):
            if path.startswith(prefix):
                rel = path[len(prefix):]
                break
        else:
            rel = path
    return "/ " + rel.replace("/", " / ")


def crumb_parts(path):
    """Individual folder segments below the source root (list)."""
    root = current_source_root(path)
    if path == root or not path.startswith(root + "/"):
        return []
    return [p for p in path[len(root) + 1:].split("/") if p]


def crumb_display_parts(parts, title, max_chars=74):
    """Breadcrumb labels to display: all segments, collapsing the MIDDLE to a single ellipsis when over max_chars (first and deepest kept; last shown full if possible). Display slots no longer equal path levels once "…" is present, so goto() maps a slot back to the real segment level."""
    if not parts:
        return []
    capped = []
    for i, p in enumerate(parts):
        is_last = i == len(parts) - 1
        if not is_last and len(p) > CRUMB_SEG_MAX:
            capped.append(p[:CRUMB_SEG_MAX] + "…")
        else:
            capped.append(p)
    def shown(d):
        return sum(3 + len(p) for p in d)
    budget = max_chars - max(0, len(title) - 10)
    if shown(capped) <= budget:
        return list(capped)
    if len(parts) == 1:
        full = capped[0]
        if len(full) + 3 <= budget:
            return [full]
        room = budget - 3 - 2
        return [full[:room] + "…"] if room > 0 else ["…"]
    last_full = capped[-1]
    if shown([capped[0], "…"]) + 3 + len(last_full) <= budget:
        disp = [capped[0], "…", last_full]
    else:
        room = budget - shown([capped[0], "…"]) - 3 - 2
        if room > 0:
            disp = [capped[0], "…", last_full[:room] + "…"]
        else:
            disp = [capped[0], "…"]
            room2 = budget - shown(disp) - 3 - 1
            if room2 > 0:
                disp.append(last_full[:room2] + "…")
            return disp
    for p in reversed(capped[1:-1]):
        if shown(disp) + 3 + len(p) <= budget:
            disp.insert(2, p)
    # safety net: truncate the longest segment if still over budget
    prev = None
    while shown(disp) > budget and disp != prev:
        prev = list(disp)
        candidates = [(i, len(disp[i])) for i, p in enumerate(disp) if p != "…" and len(p) > 1]
        if not candidates:
            break
        is_last_idx = len(disp) - 1 if disp[-1] != "…" else -1
        candidates.sort(key=lambda x: (x[0] == is_last_idx, x[1]), reverse=True)
        i = candidates[0][0]
        base = disp[i].rstrip("…")
        keep = max(1, len(base) - (shown(disp) - budget) - 1)
        disp[i] = base[:keep] + "…"
    return disp


def set_current(path):
    win = xbmcgui.Window(10000)
    # rstrip_slash, not rstrip: a bare-scheme network entry ("ftp://") must
    # keep its slashes, else bp.path loses the network branch.
    path = sources.rstrip_slash(path)
    # The chip shows the source the user ENTERED (bp.src); re-resolve only when
    # the path LEAVES the entered source (the outer source then takes over).
    if path:
        src = current_source_root(path)
        if src != sources.rstrip_slash(path_dec(win.getProperty("bp.src") or "")):
            win.setProperty("bp.src", path_enc(src))
            win.setProperty("bp.title", sources.short_label(src))
            # Chip icon follows the entered source (only on change: icon_for
            # scans all roots, too heavy for every navigation).
            try:
                win.setProperty("bp.current.icon", sources.icon_for(src))
            except Exception:
                pass
    else:
        win.clearProperty("bp.src")
        win.clearProperty("bp.current.icon")
    # Veil the list until list.py built the new items; only when the path
    # really changes, else no reload clears the veil (daemon has a timeout).
    old = sources.rstrip_slash(path_dec(win.getProperty("bp.path") or ""))
    if path != old:
        win.setProperty("bp.listload", "1")
        win.setProperty("bp.listload.t", repr(time.time()))
        win.clearProperty("bp.listload.ready")
    win.setProperty("bp.path", path_enc(path))
    # Decode network segments BEFORE truncation: truncating the encoded form
    # cuts inside %XX escapes (crumb_display_parts counts real characters).
    net = sources.is_network_path(path)
    parts = crumb_parts(path)
    if net:
        parts = [sources.url_unquote(p) for p in parts]
    win.setProperty("bp.crumb", safe_label("/ " + " / ".join(parts) if parts else ""))
    # Clickable breadcrumb slots bp.crumb.1-7; the middle-collapse ellipsis is
    # NOT a button (rendered by non-focusable label 48, bp.crumb.ellipsis).
    disp = crumb_display_parts(parts, win.getProperty("bp.title") or "")
    real = [d for d in disp if d != "…"]
    if "…" in disp and real:
        win.setProperty("bp.crumb.ellipsis", "1")
    else:
        win.clearProperty("bp.crumb.ellipsis")
    # Deepest segment = CURRENT folder: non-focusable label bp.crumb.cur;
    # buttons 40-46 hold the ANCESTOR segments only.
    cur = real[-1] if real else ""
    anc = real[:-1]
    for i in range(CRUMB_DEPTH):
        val = safe_label(anc[i]) if i < len(anc) else ""
        win.setProperty("bp.crumb.%d" % (i + 1), val)
    win.setProperty("bp.crumb.cur", safe_label(cur) if cur else "")
    log("nav: %s" % redact(path))


def nav(path):
    from fileops import open_file  # lazy: fileops importiert navigation (Zyklus)
    raw = (path or "").strip()
    if sources.is_network_path(raw):
        # VFS URLs are never os.path.isdir(): without this the click falls
        # into the open_file branch and folder navigation is impossible.
        p = sources.rstrip_slash(raw)
        win = xbmcgui.Window(10000)
        if path_dec(win.getProperty("bp.path") or "") == p:
            return
        reset_top()
        set_current(p)
        return
    p = raw.rstrip("/")
    p = fs_path(p)
    if not os.path.isdir(p):
        p = resolve_real(p)
    if not os.path.isdir(p):
        open_file(p)
        return
    win = xbmcgui.Window(10000)
    current = path_dec(win.getProperty("bp.path") or "")
    if current == p:
        return
    reset_top()
    set_current(p)


def up():
    win = xbmcgui.Window(10000)
    p = sources.rstrip_slash(path_dec(win.getProperty("bp.path") or ""))
    # The entered source root (bp.src) is root-like: no level above it.
    src = sources.rstrip_slash(path_dec(win.getProperty("bp.src") or ""))
    if src and p == src:
        return
    if sources.is_network_path(p):
        # Never walk a network URL with os.path.dirname: it turns
        # "davs://host" into "davs:". net_parent() stops at the host boundary.
        parent = sources.net_parent(p)
        if not parent or parent == p:
            return
        reset_top()
        set_current(parent)
        return
    parent = os.path.dirname(p)
    if not parent or parent == p or parent in ("/", "/Volumes"):
        return
    reset_top()
    set_current(parent)


def root():
    win = xbmcgui.Window(10000)
    p = path_dec(win.getProperty("bp.path") or "").rstrip("/")
    r = current_source_root(p)
    if r == p:
        return
    reset_top()
    set_current(r)


def goto(level):
    """Click on breadcrumb segment `level` (1-based DISPLAY SLOT): reconstruct the target from the current path. When the middle is collapsed, slot 1 shows the first segment and the remaining slots the DEEPEST segments, so a slot is mapped back to the real level."""
    try:
        level = int(level)
    except (TypeError, ValueError):
        return
    win = xbmcgui.Window(10000)
    p = path_dec(win.getProperty("bp.path") or "").rstrip("/")
    root_p = current_source_root(p)
    parts = crumb_parts(p)
    # Display mapping uses decoded segments; the target is rebuilt from raw parts.
    disp_parts = parts
    if sources.is_network_path(p):
        disp_parts = [sources.url_unquote(s) for s in parts]
    disp = crumb_display_parts(disp_parts, win.getProperty("bp.title") or "")
    real = [d for d in disp if d != "…"]
    n = len(parts)
    if level < 1 or level > len(real):
        return
    if level == 1:
        target_level = 1
    else:
        target_level = n - (len(real) - level)
    target = root_p + "/" + "/".join(parts[:target_level])
    if target == p:
        return
    reset_top()
    set_current(target)


def _current_dir():
    try:
        p = path_dec(xbmcgui.Window(10000).getProperty("bp.path") or "")
        p = p.rstrip("/")
        p = fs_path(p)
        if p and os.path.isdir(p):
            return p
    except Exception:
        pass
    return os.path.expanduser("~")

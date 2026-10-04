#!/usr/bin/env python3
"""Background worker for folder sizes (daemon started by boot.py): watches bp.path, scans top-level folders incrementally, writes folder-sizes.json and refreshes the list. Cache key = exact path; mtime change or miss recomputes. bp.foldersize.running holds the instance start timestamp (takeover token; a fresh instance takes over unless another started <3s ago).
"""
import os
import time

import xbmc
import xbmcgui
import xbmcvfs

CACHE_NAME = "folder-sizes.json"


def log(msg):
    # Sanitized sink (see list.py).
    _common_log("foldersize: " + msg)


RUNNING = "bp.foldersize.running"
# Two-phase: first pass max 1s per folder (immediate "~"), second pass exact.
INITIAL_TIMEOUT = 1.0
EXACT_TIMEOUT = 30.0  # second pass, generous but still bounded
FAST_DU_TIMEOUT = 2.0
# Network: cap the stats per folder pass (each is a server request)
NET_SCAN_MAX = 60

# Shared blacklist logic (canonical matcher in blacklist.py).
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import safe_label, redact, path_dec, natkey, read_json, write_json, state_file, cache_key, log as _common_log
from blacklist import blocked, load_active
import sources


def cache_path():
    return state_file(CACHE_NAME)


def load_cache():
    data = read_json(cache_path(), {})
    if not isinstance(data, dict):
        return {}
    # Purge pre-rename keys that carried inline credentials; rebuild by
    # credential-free key (only network entries move; first writer wins).
    clean = {}
    for k, v in data.items():
        clean.setdefault(cache_key(k), v)
    return clean


def save_cache(cache):
    try:
        # Keys must stay EXACT filesystem paths (sanitizing broke umlaut
        # lookups). write_json is atomic (unique mkstemp + os.replace).
        write_json(cache_path(), cache)
    except Exception as e:
        log("save failed: %s" % e)


def trigger_refresh(win):
    """List reload: bump the r URL param AND Container.Refresh (the URL label change alone proved unreliable). Gated on user idle (System.IdleTime(2)): otherwise set bp.foldersize.pending and flush on the next idle moment."""
    try:
        if not xbmc.getCondVisibility("System.IdleTime(2)"):
            win.setProperty("bp.foldersize.pending", "1")
            return
        win.clearProperty("bp.foldersize.pending")
        win.setProperty("bp.refresh", str(time.time()))
        xbmc.executebuiltin("Container.Refresh")
    except Exception:
        pass


def cur_path(win):
    """Decoded current folder from bp.path (stored percent-encoded)."""
    try:
        return path_dec(win.getProperty("bp.path") or "").rstrip("/")
    except Exception:
        return ""


def _player_active(win):
    """True while audio/video plays OR a network track is still opening
    (bp.aload, set before the core even starts). Network scans are skipped
    then: a flaky DAV server (CloudMe) answers 502 under concurrent requests,
    which breaks the stream open -- and Player.HasAudio is still false during
    exactly that buffering window."""
    try:
        if win is not None and win.getProperty("bp.aload") == "1":
            return True
    except Exception:
        pass
    try:
        return bool(xbmc.getCondVisibility("Player.HasAudio | Player.HasVideo"))
    except Exception:
        return False


def idle_gate(monitor, win, secs=1, cur=None):
    """Wait until the UI has been idle for `secs`; False on abort/exit/path-change. Keeps background scans from competing with navigation on slow I/O; an open photo viewer also counts as busy (a scan would jitter the animation)."""
    while not monitor.abortRequested():
        if win.getProperty("bp.exit") == "1":
            return False
        if win.getProperty("bp.photo") == "open":
            if monitor.waitForAbort(0.3):
                return False
            continue
        if cur is not None and cur_path(win) != cur:
            return False
        try:
            if xbmc.getCondVisibility("System.IdleTime(%d)" % secs):
                return True
        except Exception:
            return True
        if monitor.waitForAbort(0.3):
            return False
    return False


def folder_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def try_fast_du(path):
    """Fast estimate via `du -sk`; bytes or None on failure/timeout. Counts all files (ignores hidden/blacklist for speed), close enough for the initial display."""
    try:
        import subprocess
        res = subprocess.run(["du", "-sk", path], capture_output=True, text=True, timeout=FAST_DU_TIMEOUT)
        if res.returncode == 0:
            kb = int(res.stdout.split()[0])
            return kb * 1024
    except Exception:
        pass
    return None


def folder_size_incremental(path, show_hidden, patterns, monitor, win, cur, cache, timeout, case_sensitive=False):
    """Walk path and sum file sizes, saving intermediate results every 500 files; aborts on monitor/path/timeout. Returns (total, timed_out)."""
    total = 0
    count = 0
    start = time.time()
    timed_out = False
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            if monitor.abortRequested():
                break
            if win.getProperty("bp.exit") == "1":
                break
            if cur_path(win) != cur:
                log("path changed during scan, abort %s" % redact(path))
                break
            if time.time() - start > timeout:
                log("timeout %.1fs for %s (partial %d files, %d bytes)" % (timeout, redact(path), count, total))
                timed_out = True
                break
            dirnames[:] = [d for d in dirnames
                           if not blocked(d, patterns, case_sensitive)
                           and (show_hidden or not d.startswith("."))]
            for fname in filenames:
                if monitor.abortRequested():
                    break
                if cur_path(win) != cur:
                    break
                if time.time() - start > timeout:
                    timed_out = True
                    break
                if fname.startswith(".") and not show_hidden:
                    continue
                if blocked(fname, patterns, case_sensitive):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    total += os.path.getsize(fpath)
                except OSError:
                    pass
                count += 1
                if count % 500 == 0:
                    if cur_path(win) == cur:
                        cache[path] = {"size": total, "mtime": folder_mtime(path), "updated": time.time(), "partial": True, "approx": True}
                        save_cache(cache)
                        trigger_refresh(win)
                    xbmc.sleep(10)
                    if monitor.abortRequested():
                        break
                    if cur_path(win) != cur:
                        break
                    if time.time() - start > timeout:
                        timed_out = True
                        break
                elif count % 200 == 0:
                    xbmc.sleep(5)
            if timed_out:
                break
            xbmc.sleep(2)
    except OSError:
        pass
    return total, timed_out


def _vfs_stat(path, is_dir=False):
    """(size, mtime) for a VFS path, or None. Kodi's DAV stat returns a ZEROED mtime for collections, so for a folder the trailing-slash form is tried and st_ctime serves as fallback."""
    best = None
    for cand in ((path + "/", path) if is_dir else (path,)):
        try:
            st = xbmcvfs.Stat(cand)
            size = int(st.st_size())
            try:
                mtime = int(st.st_mtime()) or int(st.st_ctime())
            except Exception:
                mtime = int(st.st_mtime())
        except Exception:
            continue
        if mtime:
            return size, mtime
        if best is None:
            best = (size, mtime)
    if best is not None:
        return best
    try:
        return int(xbmcvfs.File(path).size()), 0
    except Exception:
        return None


# Per-folder cooldown for network scans (seconds); daemon memory only.
_NET_COOLDOWN = {}
NET_RESCAN_SECS = 120
# Effective blacklist patterns, reloaded at most every 5s.
_PATTERNS_CACHE = None
_PATTERNS_AT = 0.0


def _scan_network_files(cur, cache, win, monitor, show_hidden, patterns, case_sensitive):
    """Cache size + mtime for the entries of the current NETWORK folder: a VFS stat is a server request, so stat per row would stall the listing. Only FILES get a size; folders get their date."""
    now = time.time()
    if now - _NET_COOLDOWN.get(cur, 0) < NET_RESCAN_SECS:
        return
    # Bound the cooldown map so it cannot grow without bound.
    if len(_NET_COOLDOWN) > 64:
        cutoff = now - NET_RESCAN_SECS
        for k in [k for k, t in _NET_COOLDOWN.items() if t < cutoff]:
            _NET_COOLDOWN.pop(k, None)
    _NET_COOLDOWN[cur] = now
    try:
        res = xbmcvfs.listdir(sources.vfs_dir(cur))
    except Exception:
        return
    if not (isinstance(res, tuple) and len(res) == 2 and res[0] is not False):
        return
    base = cur.rstrip("/")
    entries = [(n, True) for n in (res[0] or [])] + [(n, False) for n in (res[1] or [])]
    # WebDAV phantom: Kodi's listing includes the folder ITSELF as a child
    # (same filter as list.py) -- never stat it.
    if cur.lower().startswith(("dav://", "davs://")):
        tail = base.rsplit("/", 1)[-1]
        if tail:
            entries = [(n, d) for (n, d) in entries if n != tail]
    changed = False
    sized = dated = failed = 0
    # WebDAV: one PROPFIND hands out sizes AND dates for the whole folder.
    # The listing tries it first; this is the long-timeout fallback for misses.
    if cur.lower().startswith(("dav://", "davs://")):
        if monitor.abortRequested() or cur_path(win) != cur:
            return
        if not idle_gate(monitor, win, 1, cur):
            return
        try:
            details = sources.dav_details(cur)
        except Exception as e:
            details = {}
            log("dav details crashed: %s" % e)
        if details:
            for key, (dsize, dmtime, ddir) in details.items():
                if not key.startswith(("dav://", "davs://")):
                    continue    # name keys only serve the lookup
                entry = {"size": 0 if ddir else dsize, "mtime": dmtime}
                if ddir:
                    entry["dir"] = True
                    entry["tried"] = True
                cache[cache_key(key.rstrip("/"))] = entry
            changed = True
    for name, is_dir in entries[:NET_SCAN_MAX]:
        if monitor.abortRequested() or cur_path(win) != cur:
            return
        disp = sources.url_unquote(name) if sources.is_network_path(cur) else name
        if disp.startswith(".") and not show_hidden:
            continue
        if blocked(disp, patterns, case_sensitive):
            continue
        child = base + "/" + name
        ckey = cache_key(child)
        cached = cache.get(ckey)
        # A folder cached WITHOUT a date is retried exactly once; the "tried"
        # flag keeps the daemon from re-requesting it every poll.
        if cached and "size" in cached and (not is_dir or cached.get("tried")):
            continue
        if not idle_gate(monitor, win, 1, cur):
            return
        st = _vfs_stat(child, is_dir)
        if st is None:
            failed += 1
            continue
        size, mtime = st
        if size:
            sized += 1
        if mtime:
            dated += 1
        if is_dir and not mtime:
            # Diagnostic (once per folder thanks to the cache).
            log("netstat: folder without date: %s" % redact(child))
        if is_dir:
            cache[ckey] = {"size": 0, "mtime": mtime, "dir": True, "tried": True}
        else:
            cache[ckey] = {"size": size, "mtime": mtime}
        changed = True
    if sized or dated or failed:
        log("netstat: %s: %d sized, %d dated, %d failed" % (redact(cur), sized, dated, failed))
    if changed:
        save_cache(cache)
        trigger_refresh(win)


def main():
    win = xbmcgui.Window(10000)
    now = time.time()
    try:
        guard_time = float(win.getProperty(RUNNING) or "0")
    except ValueError:
        guard_time = 0.0
    if guard_time and now - guard_time < 3:
        log("already running, exit")
        return
    # A fresh Kodi session (no prior token in this process) clears the cache;
    # a skin reload keeps it (window 10000 survives reloads, not a restart).
    fresh_session = guard_time == 0
    token = repr(now)
    win.setProperty(RUNNING, token)
    if fresh_session:
        log("daemon started - clearing cache for fresh session")
        try:
            p = cache_path()
            if os.path.isfile(p):
                os.remove(p)
                log("cleared previous cache %s" % p)
        except Exception as e:
            log("clear failed: %s" % e)
    else:
        log("daemon started - keeping cache (skin reload)")
    cache = {}
    monitor = xbmc.Monitor()
    last_path = None
    try:
        while not monitor.abortRequested():
            # Pre-exit handshake (main.py power()): without an addon context
            # the abort notification never arrives.
            if win.getProperty("bp.exit") == "1":
                log("bp.exit set, exiting")
                break
            if win.getProperty(RUNNING) != token:
                log("newer instance took over, exiting")
                break
            # Opt-in (default OFF): keeps slow USB sticks free.
            try:
                sizes_on = bool(xbmc.getCondVisibility("Skin.HasSetting(foldersize.on)"))
            except Exception:
                sizes_on = False
            try:
                # Network sizes/dates are their OWN setting, independent of
                # the local folder-size scan.
                netsize_on = bool(xbmc.getCondVisibility("Skin.HasSetting(netsize.on)"))
            except Exception:
                netsize_on = False
            if not sizes_on and not netsize_on:
                if monitor.waitForAbort(1.0):
                    break
                continue
            cur = cur_path(win)
            # Settings as conditions (not getInfoLabel): the canonical API.
            try:
                show_hidden = bool(xbmc.getCondVisibility("Skin.HasSetting(show.hidden)"))
            except Exception:
                show_hidden = False
            try:
                case_sensitive = bool(xbmc.getCondVisibility(
                    "Skin.HasSetting(blacklist.casesensitive)"))
            except Exception:
                case_sensitive = False
            # Patterns only change with the folder/settings: reload at most
            # every few seconds (four file reads each tick otherwise).
            global _PATTERNS_CACHE, _PATTERNS_AT
            if _PATTERNS_CACHE is None or time.time() - _PATTERNS_AT > 5.0:
                _PATTERNS_CACHE = load_active()
                _PATTERNS_AT = time.time()
            patterns = _PATTERNS_CACHE

            if (netsize_on and cur and sources.is_network_path(cur)
                    and not _player_active(win)):
                _scan_network_files(cur, cache, win, monitor, show_hidden,
                                    patterns, case_sensitive)
            if cur and os.path.isdir(cur) and cur != last_path:
                log("path changed: %s" % redact(cur))
                last_path = cur

            if sizes_on and cur and os.path.isdir(cur):
                try:
                    entries = list(os.scandir(cur))
                except OSError:
                    entries = []
                folders = []
                for e in entries:
                    try:
                        is_dir = e.is_dir()
                    except OSError:
                        continue
                    if not is_dir:
                        continue
                    name = e.name
                    if name.startswith(".") and not show_hidden:
                        continue
                    if blocked(name, patterns, case_sensitive):
                        continue
                    folders.append(e.path)

                # Phase 1: quick ~1s per folder for live display.
                pending_exact = []
                phase1_refreshed = False
                for fpath in sorted(folders, key=lambda p: natkey(safe_label(p))):
                    if monitor.abortRequested() or cur_path(win) != cur:
                        break
                    if not idle_gate(monitor, win, 1, cur):
                        break
                    mtime = folder_mtime(fpath)
                    cached = cache.get(fpath)
                    if cached and cached.get("mtime") == mtime and not cached.get("partial"):
                        # exact or final approx -> done
                        continue
                    if cached and cached.get("mtime") == mtime and cached.get("partial"):
                        # partial from interrupted quick scan -> queue for exact
                        pending_exact.append(fpath)
                        continue
                    log("quick scan %s (1s)" % redact(fpath))
                    fast = try_fast_du(fpath)
                    if fast is not None and not monitor.abortRequested() and cur_path(win) == cur:
                        cache[fpath] = {"size": fast, "mtime": mtime, "updated": time.time(), "partial": True, "approx": True}
                        save_cache(cache)
                        phase1_refreshed = True
                        monitor.waitForAbort(0.2)
                        if monitor.abortRequested() or cur_path(win) != cur:
                            break
                    size, timed_out = folder_size_incremental(fpath, show_hidden, patterns, monitor, win, cur, cache, INITIAL_TIMEOUT, case_sensitive)
                    if monitor.abortRequested() or cur_path(win) != cur:
                        break
                    # 1s always approximate
                    cache[fpath] = {"size": size, "mtime": mtime, "updated": time.time(), "approx": True, "partial": True}
                    save_cache(cache)
                    phase1_refreshed = True
                    monitor.waitForAbort(0.2)
                    pending_exact.append(fpath)
                    monitor.waitForAbort(0.1)
                if phase1_refreshed and cur_path(win) == cur:
                    trigger_refresh(win)

                # Phase 2: exact scan for those pending.
                phase2_refreshed = False
                for fpath in pending_exact:
                    if monitor.abortRequested() or cur_path(win) != cur:
                        break
                    if not idle_gate(monitor, win, 1, cur):
                        break
                    mtime = folder_mtime(fpath)
                    cached = cache.get(fpath)
                    if cached and cached.get("mtime") == mtime and not cached.get("partial"):
                        # exact or final approx -> already done
                        continue
                    log("exact scan %s" % redact(fpath))
                    size, timed_out = folder_size_incremental(fpath, show_hidden, patterns, monitor, win, cur, cache, EXACT_TIMEOUT, case_sensitive)
                    if monitor.abortRequested() or cur_path(win) != cur:
                        break
                    entry = {"size": size, "mtime": mtime, "updated": time.time()}
                    if timed_out:
                        entry["approx"] = True
                        log("exact timeout for %s: ~%d bytes" % (redact(fpath), size))
                    cache[fpath] = entry
                    save_cache(cache)
                    phase2_refreshed = True
                    monitor.waitForAbort(0.4)
                    monitor.waitForAbort(0.2)
                if phase2_refreshed and cur_path(win) == cur:
                    trigger_refresh(win)
            # flush a refresh postponed during user activity (idle >= 2s)
            if win.getProperty("bp.foldersize.pending") == "1" and xbmc.getCondVisibility("System.IdleTime(2)"):
                win.clearProperty("bp.foldersize.pending")
                trigger_refresh(win)
            if monitor.waitForAbort(1.0):
                break
    finally:
        # Clear the guard only when WE still own it (a superseded instance must
        # not erase the newer instance's token).
        if win.getProperty(RUNNING) == token:
            win.clearProperty(RUNNING)
        log("daemon stopped")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Never toast (Add-on error): log and exit; the next Home activation
        # starts a fresh instance.
        try:
            log("error: %s" % e)
        except Exception:
            pass

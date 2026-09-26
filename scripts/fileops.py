#!/usr/bin/env python3
"""File operations: open/play, rename, delete, new folder, context menu."""
import json
import hashlib
import os
import random
import shutil
import stat
import sys
import time

import xbmc
import xbmcgui
import xbmcvfs

from common import fs_path, log, L, skin_name, safe_label, redact, path_enc, path_dec, natkey, play_str, state_dir, cache_key, heic_capable
from urllib.parse import quote
import keyboard
import blacklist
import resume
import sources
from navigation import _current_dir, nav

PLAYABLE_EXT = {
    ".m4v", ".mkv", ".mp4", ".mov", ".avi", ".mpg", ".mpeg", ".webm",
    ".flv", ".wmv", ".ts", ".m2ts", ".3gp", ".ogv",
    ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".wav", ".opus",
    ".wma", ".aiff", ".aif",
}
AUDIO_EXT = {
    ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".wav", ".opus",
    ".wma", ".aiff", ".aif",
}
VIDEO_EXT = {
    ".m4v", ".mkv", ".mp4", ".mov", ".avi", ".mpg", ".mpeg", ".webm",
    ".flv", ".wmv", ".ts", ".m2ts", ".3gp", ".ogv",
}
HEIC_EXT = {".heic", ".heif", ".hif"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
             ".avif", ".heic", ".heif", ".hif"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"}

# Slideshow interval (s); OSD cycles PHOTO_INTERVALS.
PHOTO_INTERVAL = 5
PHOTO_INTERVALS = (5, 10, 15, 20)
# Recursive slideshow walk bounds (depth, file cap).
PHOTO_TREE_DEPTH = 6
PHOTO_TREE_MAX = 4000
# Interval glyphs; literal paths so the media prune keeps them.
PHOTO_INTERVAL_ICONS = {
    5: "photo/stopwatch-5.png",
    10: "photo/stopwatch-10.png",
    15: "photo/stopwatch-15.png",
    20: "photo/stopwatch-20.png",
}
# Kodi's GUI cannot rotate a JPEG, so oriented copies are cached; only huge
# sources are capped. Bump PHOTO_ORIENT_TAG to invalidate old caches.
PHOTO_MAX_DIM = 4096
PHOTO_ORIENT_TAG = "v2full"
# Ken Burns texture cap floor (1080p); _kb_cap() scales it up for higher res.
PHOTO_KB_DIM = 2560
# Each Ken Burns corner variant starts/ends at centre, so flipping at the
# cycle boundary is seamless.
PHOTO_KB_MODE = 2
PHOTO_KB_CORNERS = 4
PHOTO_KB_CYCLE = 24.0
# VFS file ops: network sources are Kodi VFS URLs, so os.path.* fails for
# them; existence/type checks and mutations go through xbmcvfs.


def _net(path):
    return sources.is_network_path(path)


def _exists(path):
    """Existence check that also works for VFS network URLs."""
    if _net(path):
        try:
            return bool(xbmcvfs.exists(path))
        except Exception:
            return False
    return os.path.exists(path)


def _is_dir(path):
    """Directory check for local paths and VFS network URLs.

    Do NOT list the item itself: WebDAV answers a PROPFIND on a FILE with
    200/207, so list the PARENT and test its DIRECTORY list."""
    if _net(path):
        base = path.rstrip("/")
        parent, _sep, name = base.rpartition("/")
        if not parent or not name:
            return False
        try:
            res = xbmcvfs.listdir(parent)
            if isinstance(res, tuple) and len(res) == 2 and res[0] is not False:
                return name in list(res[0] or [])
        except Exception:
            pass
        return False
    return os.path.isdir(path)


def _vfs_rmtree(path):
    """Recursively remove a network folder via the VFS. Returns True on success.

    Kodi's WebDAV listing returns the folder itself as a phantom child; skip
    it to avoid infinite recursion. A falsey delete/rmdir is only a failure when
    the target still exists (a vanished entry / no-op backend must not error)."""
    base = path.rstrip("/")
    tail = base.rsplit("/", 1)[-1]
    dirs, files = [], []
    try:
        res = xbmcvfs.listdir(base)
        if isinstance(res, tuple) and len(res) == 2 and res[0] is not False:
            dirs = list(res[0] or [])
            files = list(res[1] or [])
    except Exception:
        # A transient listing failure must not abort: still try the children
        # that are known and the (possibly empty) rmdir pass below.
        dirs, files = [], []
    ok = True
    for f in files:
        if f.rstrip("/") == tail:
            continue
        child = base + "/" + f
        try:
            done = bool(xbmcvfs.delete(child))
        except Exception:
            done = False
        if not done and _vfs_exists(child):
            ok = False
    for d in dirs:
        if d.rstrip("/") == tail:
            continue
        if not _vfs_rmtree(base + "/" + d):
            ok = False
    try:
        done = bool(xbmcvfs.rmdir(base))
    except Exception:
        done = False
    if not done and _vfs_exists(base):
        ok = False
    return ok


def _vfs_exists(path):
    try:
        return bool(xbmcvfs.exists(path))
    except Exception:
        return True  # unknown: assume it is still there (report the failure)


def _vfs_delete(path):
    if _is_dir(path):
        return _vfs_rmtree(path)
    try:
        if xbmcvfs.delete(path):
            return True
    except Exception:
        pass
    return not _vfs_exists(path)


def _display_name(path):
    """Item name for a modal header. Decode the FULL path first: the basename
    alone has no scheme, so `url_display` would leave `%20` in place."""
    disp = sources.url_display(path).rstrip("/")
    return safe_label(os.path.basename(disp) or disp)


def rename(path=None):
    """With path: open our keyboard pre-filled. Without: run the pending rename."""
    win = xbmcgui.Window(10000)
    if path:
        p = path.strip()
        if not p or not _exists(p):
            xbmcgui.Dialog().notification(skin_name(), L(31330), xbmcgui.NOTIFICATION_ERROR, 3000)
            _focus_list()
            return
        keyboard.open_kb("rename", path=p)
        return
    p = path_dec(win.getProperty("bp.rename.path") or "").strip()
    new_name = (win.getProperty("bp.rename.name") or "").strip()
    win.clearProperty("bp.rename.path")
    win.clearProperty("bp.rename.name")
    if not p or not new_name:
        return
    net = _net(p)
    base = os.path.basename(p.rstrip("/"))
    # Kodi's VFS does NOT re-encode a space, so compare decoded and re-encode.
    if new_name == (sources.url_unquote(base) if net else base):
        return
    # prevent path traversal
    if "/" in new_name or "\\" in new_name:
        xbmcgui.Dialog().notification(skin_name(), L(31331), xbmcgui.NOTIFICATION_ERROR, 3000)
        return
    target_name = quote(new_name, safe="") if net else new_name
    new_path = os.path.join(os.path.dirname(p.rstrip("/")), target_name)
    if _exists(new_path):
        xbmcgui.Dialog().notification(skin_name(), L(31334), xbmcgui.NOTIFICATION_ERROR, 3000)
        return
    try:
        if net:
            xbmcvfs.rename(p, new_path)
        else:
            os.rename(p, new_path)
        log("rename: %s -> %s" % (redact(p), redact(new_path)))
        xbmc.executebuiltin("Container.Refresh")
        win.setProperty("bp.refresh", str(time.time()))
    except Exception as e:
        log("rename failed: %s" % e)
        xbmcgui.Dialog().notification(skin_name(), safe_label(L(31336) % e), xbmcgui.NOTIFICATION_ERROR, 4000)


def ctx(path):
    """Open our own context menu overlay (bp.ctx) for `path`.

    bp.ctx.path is percent-encoded TWICE: Kodi decodes skin RunScript args
    once with single-byte semantics, so double encoding preserves exact bytes."""
    p = (path or "").strip()
    if not p or not _exists(p):
        return
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") == "open":
        # Never open the context menu behind the photo viewer.
        return
    name = _display_name(p)
    if len(name) > 25:
        name = name[:24] + "…"
    win.setProperty("bp.ctx.path", quote(quote(p, safe="", errors="surrogateescape"), safe=""))
    win.setProperty("bp.ctx.title", name)
    win.clearProperty("bp.ctx.reduced")
    # Copy/cut/paste are local-only: no reliable recursive VFS copy yet.
    if _net(p):
        win.setProperty("bp.ctx.net", "1")
    else:
        win.clearProperty("bp.ctx.net")
    # ftp/ftps is read-only: grey those rows and focus Cancel (450).
    ro = sources.is_readonly(p)
    if ro:
        win.setProperty("bp.ctx.readonly", "1")
    else:
        win.clearProperty("bp.ctx.readonly")
    win.setProperty("bp.ctx", "open")
    # Close the native relay dialog so keys reach the overlay, then focus it.
    xbmc.executebuiltin("Dialog.Close(10106)")
    time.sleep(0.3)
    xbmc.executebuiltin("SetFocus(%d)" % (450 if ro else 121))
    log("ctx: %s" % redact(p))


def ctx_current():
    """Open the reduced context menu (new folder + cancel) for the current folder."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") == "open":
        # Never open the context menu behind the photo viewer.
        return
    cur = path_dec(win.getProperty("bp.path") or "").rstrip("/")
    if _net(cur):
        win.setProperty("bp.ctx.net", "1")
    else:
        win.clearProperty("bp.ctx.net")
    # ftp/ftps: "New folder" is unsupported -> greyed, focus Cancel (452).
    ro = sources.is_readonly(cur)
    if ro:
        win.setProperty("bp.ctx.readonly", "1")
    else:
        win.clearProperty("bp.ctx.readonly")
    win.clearProperty("bp.ctx.path")
    win.setProperty("bp.ctx.reduced", "1")
    win.setProperty("bp.ctx", "open")
    xbmc.executebuiltin("Dialog.Close(10106)")
    time.sleep(0.3)
    xbmc.executebuiltin("SetFocus(%d)" % (452 if ro else 451))
    log("ctx: current folder (reduced menu)")


def mkdircreate():
    """Keyboard-OK handler: create the folder named in bp.mkdir.name."""
    win = xbmcgui.Window(10000)
    name = (win.getProperty("bp.mkdir.name") or "").strip()
    win.clearProperty("bp.mkdir.name")
    if not name:
        return
    if "/" in name or "\\" in name:
        xbmcgui.Dialog().notification(skin_name(), L(31331), xbmcgui.NOTIFICATION_ERROR, 3000)
        return
    # Network folder: read bp.path directly (_current_dir uses os.path.isdir)
    # and percent-encode the name (Kodi's VFS does not re-encode a space).
    cur_raw = path_dec(win.getProperty("bp.path") or "").rstrip("/")
    net = _net(cur_raw)
    if net:
        new_path = cur_raw + "/" + quote(name, safe="")
        if _exists(new_path):
            xbmcgui.Dialog().notification(skin_name(), L(31335), xbmcgui.NOTIFICATION_ERROR, 3000)
            return
        try:
            # mkdir, NOT mkdirs: the recursive variant does not know VFS
            # protocols and fails silently for davs://. Returns False on failure.
            if not xbmcvfs.mkdir(new_path):
                raise OSError("VFS mkdir failed")
            log("mkdir: %s" % redact(new_path))
            xbmc.executebuiltin("Container.Refresh")
            win.setProperty("bp.refresh", str(time.time()))
        except Exception as e:
            log("mkdir failed: %s" % e)
            xbmcgui.Dialog().notification(skin_name(), safe_label(L(31338) % e), xbmcgui.NOTIFICATION_ERROR, 4000)
        return
    cur = _current_dir()
    new_path = os.path.join(cur, name)
    if os.path.exists(new_path):
        xbmcgui.Dialog().notification(skin_name(), L(31335), xbmcgui.NOTIFICATION_ERROR, 3000)
        return
    try:
        os.makedirs(new_path)
        log("mkdir: %s" % redact(new_path))
        xbmc.executebuiltin("Container.Refresh")
        win.setProperty("bp.refresh", str(time.time()))
    except Exception as e:
        log("mkdir failed: %s" % e)
        xbmcgui.Dialog().notification(skin_name(), safe_label(L(31338) % e), xbmcgui.NOTIFICATION_ERROR, 4000)


def _rmtree(path):
    """shutil.rmtree that survives macOS AppleDouble "._*"/.DS_Store files
    (read-only or recreated mid-delete). Swallows metadata-only failures."""
    def _handle(func, p, exc):
        err = exc if isinstance(exc, BaseException) else exc[1]
        try:
            os.chmod(p, stat.S_IRWXU)
        except OSError:
            pass
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            func(p)
            return
        except OSError:
            pass
        if os.path.basename(p).startswith("._") or os.path.basename(p) == ".DS_Store":
            return
        raise err

    kw = {"onexc": _handle} if sys.version_info >= (3, 12) else {"onerror": _handle}
    shutil.rmtree(path, **kw)


def delete(path):
    """Arm the delete confirmation overlay (bp.del); real deletion in delconfirm()."""
    p = (path or "").strip()
    if not p or not _exists(p):
        xbmcgui.Dialog().notification(skin_name(), L(31330), xbmcgui.NOTIFICATION_ERROR, 3000)
        _focus_list()
        return
    name = _display_name(p)
    win = xbmcgui.Window(10000)
    win.setProperty("bp.del.path", path_enc(p))
    # Header shows the item name with the looping ticker.
    win.setProperty("bp.del.title", name)
    win.setProperty("bp.del.title.rep", safe_label(_ticker(name, 11, 560)))
    win.setProperty("bp.del.line", L(31332) if _is_dir(p) else L(31333))
    win.setProperty("bp.del", "open")
    # Focus the safe default (No), delayed so a hiding trigger overlay cannot
    # refocus away. The legacy SDK ControlButton has no setFocus().
    time.sleep(0.4)
    xbmc.executebuiltin("SetFocus(124)")
    log("delete armed: %s" % redact(p))


def delconfirm():
    """Yes-button of the delete overlay: delete bp.del.path."""
    win = xbmcgui.Window(10000)
    p = path_dec(win.getProperty("bp.del.path") or "").strip()
    win.clearProperty("bp.del")
    if not p or not _exists(p):
        return
    try:
        if _net(p):
            if not _vfs_delete(p):
                raise IOError("VFS delete failed")
        elif os.path.isdir(p):
            _rmtree(p)
        else:
            os.remove(p)
        log("delete: %s" % redact(p))
        xbmc.executebuiltin("Container.Refresh")
        win.setProperty("bp.refresh", str(time.time()))
    except Exception as e:
        log("delete failed: %s" % e)
        xbmcgui.Dialog().notification(skin_name(), safe_label(L(31337) % e), xbmcgui.NOTIFICATION_ERROR, 4000)
    # Focus back to the file list (lost focus = stuck navigation); delayed
    # like the delete arm (list rebuilds after refresh).
    time.sleep(0.3)
    xbmc.executebuiltin("SetFocus(33)")


# ---------------------------------------------------------------- copy / move

def _focus_list(delay=0.25):
    """Return focus to the file list. Every exit path of copy/move must call
    this: the launching overlay closes and focus can land on a hidden control."""
    time.sleep(delay)
    for _ in range(8):
        xbmc.executebuiltin("SetFocus(33)")
        try:
            if xbmc.getCondVisibility("Control.HasFocus(33)"):
                break
        except Exception:
            break
        time.sleep(0.15)


def _clip_set(path, mode):
    """Store `path` on the clipboard (mode 'copy'|'move') in window props."""
    p = (path or "").strip()
    if not p or not os.path.exists(p):
        xbmcgui.Dialog().notification(skin_name(), L(31330), xbmcgui.NOTIFICATION_ERROR, 3000)
        _focus_list()
        return
    name = safe_label(os.path.basename(p.rstrip("/")) or p)
    win = xbmcgui.Window(10000)
    win.setProperty("bp.clip.path", path_enc(p))
    win.setProperty("bp.clip.mode", mode)
    win.setProperty("bp.clip.name", name)
    xbmcgui.Dialog().notification(
        skin_name(),
        L(31420 if mode == "copy" else 31421) % name,
        xbmcgui.NOTIFICATION_INFO, 2000)
    log("clipboard %s: %s" % (mode, redact(p)))
    _focus_list()


def clip_copy(path):
    """Mark `path` as the copy source."""
    _clip_set(path, "copy")


def clip_cut(path):
    """Mark `path` as the move source (cut)."""
    _clip_set(path, "move")


_PROG_SEGMENTS = 20
_PROG_FILLED = [-1]  # segments currently set (avoid redundant property writes)


class _ProgCancelled(Exception):
    """Raised by the copy loop when the user hits Cancel on the progress modal."""


def _prog_cancelled():
    try:
        return xbmcgui.Window(10000).getProperty("bp.prog.cancel") == "1"
    except Exception:
        return False


def _prog_clear_segments(win):
    for i in range(1, _PROG_SEGMENTS + 1):
        win.clearProperty("bp.prog.f%d" % i)


def _prog_open(title):
    """Show the progress overlay and focus its modal click-eater (910) so the
    list gets no input during the copy."""
    win = xbmcgui.Window(10000)
    win.setProperty("bp.prog.title", title)
    win.setProperty("bp.prog.pct", "0")
    _prog_clear_segments(win)
    _PROG_FILLED[0] = 0
    win.clearProperty("bp.prog.state")
    win.clearProperty("bp.prog.msg")
    win.setProperty("bp.prog", "open")
    win.clearProperty("bp.prog.cancel")
    time.sleep(0.15)
    xbmc.executebuiltin("SetFocus(912)")


def _prog_set(pct):
    """Update the bar + percentage. Segments use String.IsEqual: numeric
    functions like IntegerLessThan do NOT evaluate window properties in <visible>."""
    try:
        pct = max(0, min(100, int(pct)))
    except (TypeError, ValueError):
        pct = 0
    win = xbmcgui.Window(10000)
    win.setProperty("bp.prog.pct", str(pct))
    filled = (pct * _PROG_SEGMENTS) // 100
    if filled == _PROG_FILLED[0]:
        return
    _PROG_FILLED[0] = filled
    for i in range(1, _PROG_SEGMENTS + 1):
        k = "bp.prog.f%d" % i
        if i <= filled:
            win.setProperty(k, "1")
        else:
            win.clearProperty(k)


def _prog_close():
    win = xbmcgui.Window(10000)
    win.clearProperty("bp.prog")
    win.clearProperty("bp.prog.state")
    win.clearProperty("bp.prog.msg")
    win.clearProperty("bp.prog.title")
    win.clearProperty("bp.prog.pct")
    win.clearProperty("bp.prog.cancel")
    _prog_clear_segments(win)
    _PROG_FILLED[0] = -1
    _focus_list()


def _prog_error(msg):
    """Switch the overlay to the error state (message + OK button) and focus OK."""
    win = xbmcgui.Window(10000)
    _prog_set(100)
    win.setProperty("bp.prog.state", "error")
    win.setProperty("bp.prog.msg", msg)
    time.sleep(0.2)
    for _ in range(8):
        xbmc.executebuiltin("SetFocus(911)")
        try:
            if xbmc.getCondVisibility("Control.HasFocus(911)"):
                break
        except Exception:
            break
        time.sleep(0.15)


def _tree_size(path):
    """Total bytes under `path` (file -> its size). Used for the progress bar."""
    if os.path.isdir(path):
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _copy_file(src, dst, state):
    """Chunked copy that advances the bar while a big file is written."""
    total = state.get("total") or 0
    last = 0.0
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            chunk = fsrc.read(1048576)  # 1 MB
            if not chunk:
                break
            if _prog_cancelled():
                raise _ProgCancelled()
            fdst.write(chunk)
            state["done"] += len(chunk)
            now = time.time()
            if total and now - last >= 0.1:
                last = now
                _prog_set(state["done"] * 100 // total)
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass
    _prog_set(state["done"] * 100 // total if total else 100)


def _copy_tree(src, dst, state):
    """Recursive copy that updates bp.prog.pct after every file/chunk."""
    if os.path.isdir(src) and not os.path.islink(src):
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            if _prog_cancelled():
                raise _ProgCancelled()
            _copy_tree(os.path.join(src, name), os.path.join(dst, name), state)
    else:
        _copy_file(src, dst, state)


def clip_paste():
    """Paste the clipboard into the current folder, showing bp.prog.

    Refuses to overwrite an existing target or move a folder into itself.
    Clears the clipboard after a successful paste."""
    win = xbmcgui.Window(10000)
    src = path_dec(win.getProperty("bp.clip.path") or "").strip()
    mode = win.getProperty("bp.clip.mode") or ""
    if not src or mode not in ("copy", "move"):
        xbmcgui.Dialog().notification(skin_name(), L(31418), xbmcgui.NOTIFICATION_ERROR, 3000)
        _focus_list()
        return
    if not os.path.exists(src):
        xbmcgui.Dialog().notification(skin_name(), L(31330), xbmcgui.NOTIFICATION_ERROR, 3000)
        _focus_list()
        return
    dest = _current_dir()
    if not dest or not os.path.isdir(dest):
        _focus_list()
        return
    src_abs = os.path.abspath(src)
    dest_abs = os.path.abspath(dest)
    name = os.path.basename(src.rstrip("/")) or "?"
    target = os.path.join(dest, name)
    title = L(31417)
    if os.path.exists(target) or src_abs == dest_abs \
            or dest_abs.startswith(src_abs + os.sep):
        _prog_open(title)
        _prog_error(L(31419))
        return
    _prog_open(title)
    state = {"total": _tree_size(src), "done": 0}
    try:
        if mode == "move":
            try:
                same_dev = os.stat(src).st_dev == os.stat(dest).st_dev
            except OSError:
                same_dev = False
            if same_dev:
                os.rename(src, target)  # instant (no copy)
                _prog_set(100)
            else:
                _copy_tree(src, target, state)
                if os.path.isdir(target) and not os.path.islink(target):
                    _rmtree(src)
                else:
                    os.remove(src)
        else:
            _copy_tree(src, target, state)
        _prog_set(100)
        log("clipboard paste (%s): %s -> %s" % (mode, redact(src), redact(target)))
        win.clearProperty("bp.clip.path")
        win.clearProperty("bp.clip.mode")
        win.clearProperty("bp.clip.name")
        time.sleep(0.35)  # let the full bar show before closing
        _prog_close()
        # Refresh AFTER the modal closes: Container.Refresh while it was up
        # was ignored, so the copied item stayed invisible.
        win.setProperty("bp.refresh", str(time.time()))
        xbmc.executebuiltin("Container.Refresh")
        time.sleep(0.2)
        xbmc.executebuiltin("SetFocus(33)")
    except _ProgCancelled:
        log("clipboard paste cancelled: %s -> %s" % (redact(src), redact(target)))
        # Remove the partial copy (a move only deletes the source after a full copy).
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                _rmtree(target)
            elif os.path.exists(target):
                os.remove(target)
        except OSError:
            pass
        _prog_close()
        return
    except Exception as e:
        log("paste failed: %s" % e)
        # Remove the partial copy: it would make the next paste fail on the
        # exists-check. The source is untouched.
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                _rmtree(target)
            elif os.path.exists(target):
                os.remove(target)
        except OSError:
            pass
        _prog_error(safe_label(L(31422) % e))


def mkdir():
    """Open our keyboard for a new folder (execution via mkdircreate)."""
    win = xbmcgui.Window(10000)
    win.clearProperty("bp.mkdir.name")
    keyboard.open_kb("mkdir")


def _set_player_osd_lines(prefix, path):
    """Set the shared player OSD lines on Window 10000 (title = file name,
    path = breadcrumb). prefix = "bp.audio" / "bp.video"."""
    win = xbmcgui.Window(10000)
    # Display only: decode the percent-encoded VFS URL (playback keeps URL form).
    path = sources.url_display(path)
    name = safe_label(os.path.basename(path))
    disp = sources.display_path_below_source(os.path.dirname(path) or ".")
    leaf = safe_label(os.path.basename((os.path.dirname(path) or ".").rstrip("/")) or "")
    if leaf in ("", ".", "/"):
        leaf = disp
    win.setProperty("%s.title" % prefix, name)
    win.setProperty("%s.title.rep" % prefix, _ticker(name, 20))
    win.setProperty("%s.path" % prefix, disp)
    win.setProperty("%s.path.rep" % prefix, _ticker(disp, 11))
    win.setProperty("%s.folder" % prefix, leaf)
    win.setProperty("%s.folder.rep" % prefix, _ticker(leaf, 11))


def _folder_playlist(folder, exts):
    """Playlist file paths of `folder`, in browser order.

    Network folders are listed through xbmcvfs (os.listdir raises); sort by
    display name so network order matches the browser list."""
    net = sources.is_network_path(folder)
    names = []
    if net:
        try:
            res = xbmcvfs.listdir(folder)
            if isinstance(res, tuple) and len(res) == 2 and res[0] is not False:
                names = list(res[1] or [])
        except Exception:
            names = []
    else:
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
    patterns = blacklist.load_active()
    case_sensitive = bool(xbmc.getCondVisibility("Skin.HasSetting(blacklist.casesensitive)"))
    picked = []
    for n in names:
        disp = sources.url_unquote(n) if net else n
        if disp.startswith(".") or blacklist.blocked(disp, patterns, case_sensitive):
            continue
        if os.path.splitext(disp)[1].lower() not in exts:
            continue
        picked.append((natkey(safe_label(disp)), os.path.join(folder, n)))
    picked.sort(key=lambda t: t[0])
    return [f for _, f in picked]


def _play_audio_folder(path):
    """Start an audio file within its folder's playlist (so Previous/Next walk
    the folder); playback starts at the selected track's position."""
    folder = os.path.dirname(path) or "."
    t0 = time.time()
    files = _folder_playlist(folder, AUDIO_EXT)
    t1 = time.time()
    _set_player_osd_lines("bp.audio", path)
    pl = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
    pl.clear()
    for f in files:
        # Labeled ListItem: an unlabeled entry makes the core probe the file
        # for a title (blocks on large network files). Path/label sanitized.
        pl.add(play_str(f), xbmcgui.ListItem(
            safe_label(os.path.basename(sources.url_display(f)))))
    t2 = time.time()
    try:
        idx = files.index(path)
    except ValueError:
        idx = -1
    if idx >= 0:
        xbmc.Player().play(pl, startpos=idx)
        t3 = time.time()
        log("audio playlist: %d tracks from %s, start '%s' @%d "
            "(list %.1fs, add %.1fs, play %.1fs)"
            % (len(files), redact(folder), redact(path), idx,
               t1 - t0, t2 - t1, t3 - t2))
    else:
        # clicked file filtered out of the folder list: play it standalone
        xbmc.Player().play(play_str(path))
        t3 = time.time()
        log("audio: standalone start '%s' (not in folder list, play %.1fs)"
            % (redact(path), t3 - t2))


def _ticker(text, char_px, width=1100):
    """Repeat text with "   |   " separators so scrolling outlasts Kodi's
    hard-coded 2-loop limit (SetScrollLoopCount(2)); short lines stay static.
    char_px = rough average pixel width per character."""
    if not text:
        return ""
    unit = max(1, len(text)) * char_px + 60
    if unit <= width:
        return text
    copies = max(2, min(100, -(-28800 // unit)))
    return "   |   ".join([text] * copies)


def _play_video_folder(path):
    """Start a video file within its folder's playlist (like audio), so
    VideoOSD Previous/Next work; entries carry a labeled ListItem."""
    folder = os.path.dirname(path) or "."
    t0 = time.time()
    files = _folder_playlist(folder, VIDEO_EXT)
    t1 = time.time()
    _set_player_osd_lines("bp.video", path)
    pl = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
    pl.clear()
    for f in files:
        # Labeled ListItem: raw path items make VideoPlayer.Title fall back
        # to the full path. Path/label sanitized; url_display decodes names.
        pl.add(play_str(f), xbmcgui.ListItem(
            safe_label(os.path.basename(sources.url_display(f)))))
    try:
        idx = files.index(path)
    except ValueError:
        idx = -1
    t2 = time.time()
    if idx >= 0 and len(files) > 1:
        xbmc.Player().play(pl, startpos=idx)
        t3 = time.time()
        log("video playlist: %d tracks from %s, start '%s' @%d "
            "(list %.1fs, add %.1fs, play %.1fs)"
            % (len(files), redact(folder), redact(path), idx,
               t1 - t0, t2 - t1, t3 - t2))
    else:
        # single video or not in list: fall back to standalone play.
        if idx >= 0:
            xbmc.Player().play(pl, startpos=idx)
            t3 = time.time()
            log("video playlist: %d tracks from %s, start '%s' @%d (single, "
                "list %.1fs, add %.1fs, play %.1fs)"
                % (len(files), redact(folder), redact(path), idx,
                   t1 - t0, t2 - t1, t3 - t2))
        else:
            xbmc.executebuiltin("PlayMedia(%s)" % json.dumps(play_str(path), ensure_ascii=False))
            log("video: standalone start '%s' (not in folder list)" % redact(path))


# Audio-focus jump window after Enter on an audio file (seconds)
FOCUS_TOTAL = 4.0
FOCUS_POLL = 0.1


def play_focus_target():
    """Footer play/pause button control id."""

    return 338


def _playback_audio():
    return xbmc.getCondVisibility("Player.HasAudio + !Player.HasVideo")


# Footer transport/state ids; focusplay never yanks focus off these.
FOCUS_TRANSPORT_IDS = (337, 338, 339, 340, 341, 342,
                       353, 354, 355)


def focusplay():
    """AlarmClock fire-time focus jump; never yanks off an already-focused
    footer control (the net can fire while the user navigates the transport row)."""
    if not _playback_audio():
        return
    for tid in FOCUS_TRANSPORT_IDS:
        if xbmc.getCondVisibility("Control.HasFocus(%d)" % tid):
            log("playback: focusplay skipped (footer control %d focused)" % tid)
            return
    end = time.time() + 1.5
    target = 338
    while True:
        target = play_focus_target()
        xbmc.executebuiltin("SetFocus(%d)" % target)
        time.sleep(FOCUS_POLL)
        if xbmc.getCondVisibility("Control.HasFocus(%d)" % target) or time.time() > end:
            break
    log("playback: focusplay SetFocus %d" % target)


def _focus_play_button():
    """Jump focus to the footer play/pause after Enter on an audio file.

    AlarmClock bp_focus is the safety net, cancelled on verified focus; the
    audio loading overlay (bp.aload) is owned by the home daemon."""
    try:
        xbmc.executebuiltin("CancelAlarm(bp_focus,True)")
        xbmc.executebuiltin(
            "AlarmClock(bp_focus,RunScript(special://skin/scripts/main.py,focusplay),00:02,silent)")
    except Exception:
        pass
    end = time.time() + FOCUS_TOTAL
    while not _playback_audio():
        if time.time() > end:
            log("playback: focus aborted (playback did not start)")
            return
        time.sleep(FOCUS_POLL)
    target = play_focus_target()
    for _ in range(5):
        # Never fight the user: stop once any footer control holds focus.
        for tid in FOCUS_TRANSPORT_IDS:
            try:
                if xbmc.getCondVisibility("Control.HasFocus(%d)" % tid):
                    log("playback: focus skipped (footer control %d focused)" % tid)
                    return
            except Exception:
                break
        xbmc.executebuiltin("SetFocus(%d)" % target)
        time.sleep(0.2)
        try:
            if xbmc.getCondVisibility("Control.HasFocus(%d)" % target):
                break
        except Exception:
            break
        if not _playback_audio():
            log("playback: focus aborted (playback stopped)")
            return
    else:
        log("playback: SetFocus %d failed, AlarmClock bp_focus retries" % target)
        return
    # Cancel the net immediately on verified focus, so the 2s alarm cannot
    # yank focus back while the user navigates.
    try:
        xbmc.executebuiltin("CancelAlarm(bp_focus,True)")
    except Exception:
        pass
    log("playback: focus on %d" % target)


# Video OSD focus -------------------------------------------------------------

def _playback_video():
    return xbmc.getCondVisibility("Player.HasVideo")


VIDEO_FOCUS_IDS = (600, 601, 602, 603, 606, 607, 510, 511, 512, 513, 344, 87)


def focusvideo():
    """AlarmClock fire-time focus jump for video; activates VideoOSD then
    focuses play/pause (602). Skips if a transport control already has focus."""
    if not _playback_video():
        return
    for tid in VIDEO_FOCUS_IDS:
        if xbmc.getCondVisibility("Control.HasFocus(%d)" % tid):
            log("video: focusvideo skipped (control %d focused)" % tid)
            return
    if not xbmc.getCondVisibility("Window.IsActive(VideoOSD)"):
        # Fullscreen first (Python play() does not auto-switch), then the OSD.
        _wait_busy_closed()
        if not xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"):
            xbmc.executebuiltin("Action(FullScreen)")
            time.sleep(0.4)
        xbmc.executebuiltin("ActivateWindow(VideoOSD)")
        time.sleep(0.3)
    end = time.time() + 1.5
    target = 602
    while True:
        xbmc.executebuiltin("SetFocus(%d)" % target)
        time.sleep(FOCUS_POLL)
        if xbmc.getCondVisibility("Control.HasFocus(%d)" % target) or time.time() > end:
            break
    log("video: focusvideo SetFocus %d" % target)


# Upper bound for the busy wait; a sick server can hold the busy spinner for
# many seconds.
BUSY_TIMEOUT = 10.0


def _wait_busy_closed(timeout=BUSY_TIMEOUT):
    """Wait until the core busy dialog is gone (bounded).

    Window activations into an active modal are refused and play Kodi's
    error sound; returns early on playback stop or fullscreen. Never blocks
    past the timeout."""
    end = time.time() + max(0.0, timeout)
    while time.time() < end:
        try:
            if not xbmc.getCondVisibility("Window.IsActive(busydialog)"):
                return
            if not _playback_video():
                return
            if xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"):
                return
        except Exception:
            return
        time.sleep(FOCUS_POLL)
    try:
        if xbmc.getCondVisibility("Window.IsActive(busydialog)"):
            log("video: busy still active, activating anyway")
    except Exception:
        pass


def _focus_video_button():
    """Jump focus to VideoOSD play/pause after Enter on a video file.

    Waits for Player.HasVideo and the busy dialog, then fullscreen + VideoOSD
    and SetFocus 602; AlarmClock bp_video_focus is the late safety net."""
    try:
        xbmc.executebuiltin("CancelAlarm(bp_video_focus,True)")
        xbmc.executebuiltin(
            "AlarmClock(bp_video_focus,RunScript(special://skin/scripts/main.py,focusvideo),00:06,silent)")
    except Exception:
        pass
    end = time.time() + FOCUS_TOTAL
    while not _playback_video():
        if time.time() > end:
            log("video: focus aborted (playback did not start)")
            return
        time.sleep(FOCUS_POLL)
    # Python play() does not auto-switch to fullscreen like PlayMedia, so
    # leave Home explicitly; wait for the busy spinner first.
    _wait_busy_closed()
    if not xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"):
        xbmc.executebuiltin("Action(FullScreen)")
        time.sleep(0.4)
    # Keyboard PlayMedia does not auto-show the OSD.
    if not xbmc.getCondVisibility("Window.IsActive(VideoOSD)"):
        xbmc.executebuiltin("ActivateWindow(VideoOSD)")
        time.sleep(0.4)
    target = 602
    focused = False
    while time.time() < end:
        xbmc.executebuiltin("SetFocus(%d)" % target)
        time.sleep(FOCUS_POLL)
        if xbmc.getCondVisibility("Control.HasFocus(%d)" % target):
            focused = True
            break
        if not _playback_video():
            log("video: focus aborted (playback stopped)")
            return
    if not focused:
        log("video: SetFocus %d failed, AlarmClock bp_video_focus retries" % target)
        return
    try:
        xbmc.executebuiltin("CancelAlarm(bp_video_focus,True)")
    except Exception:
        pass
    log("video: focus on %d" % target)


def _playing_file():
    """Current player file (VFS URL for network items), or ''."""
    try:
        return xbmc.getInfoLabel("Player.FilenameAndPath") or ""
    except Exception:
        return ""


def audio_step(which):
    """Previous/Next audio track; raise bp.aload first for network sources so
    it paints before the core blocks."""
    try:
        cur = _playing_file()
    except Exception:
        cur = ""
    if cur and sources.is_network_path(cur):
        win = xbmcgui.Window(10000)
        win.setProperty("bp.aload", "1")
        win.setProperty("bp.aload.t", repr(time.time()))
        log("audio step %s (network, overlay raised)" % which.lower())
        time.sleep(0.4)
    xbmc.executebuiltin("PlayerControl(%s)" % which)


def audio_prev():
    """RunScript entry: previous audio track (footer + remotes)."""
    audio_step("Previous")


def audio_next():
    """RunScript entry: next audio track (footer + remotes)."""
    audio_step("Next")


def _photo_exts():
    """Image extensions the slideshow may show; HEIC dropped when not decodable."""
    if heic_capable():
        return IMAGE_EXT
    return IMAGE_EXT - HEIC_EXT


def _photo_playlist_tree(folder, exts, _depth=0):
    """Recursive folder playlist: own files first, then subfolders in name
    order. Bounded by depth and a total cap."""
    if _depth > PHOTO_TREE_DEPTH:
        return []
    net = sources.is_network_path(folder)
    dirs, files = [], []
    if net:
        try:
            res = xbmcvfs.listdir(folder)
            if isinstance(res, tuple) and len(res) == 2 and res[0] is not False:
                dirs = list(res[0] or [])
                files = list(res[1] or [])
        except Exception:
            pass
    else:
        try:
            for n in os.listdir(folder):
                try:
                    if os.path.isdir(os.path.join(folder, n)):
                        dirs.append(n)
                    else:
                        files.append(n)
                except OSError:
                    files.append(n)
        except OSError:
            pass
    patterns = blacklist.load_active()
    case_sensitive = bool(xbmc.getCondVisibility(
        "Skin.HasSetting(blacklist.casesensitive)"))

    def disp(n):
        return sources.url_unquote(n) if net else n

    def ok(n):
        d = disp(n)
        return not (d.startswith(".") or blacklist.blocked(d, patterns, case_sensitive))

    out = []
    for n in sorted(files, key=lambda x: natkey(safe_label(disp(x)))):
        if not ok(n) or os.path.splitext(disp(n))[1].lower() not in exts:
            continue
        out.append(os.path.join(folder, n))
    for d in sorted(dirs, key=lambda x: natkey(safe_label(disp(x)))):
        if not ok(d):
            continue
        out.extend(_photo_playlist_tree(os.path.join(folder, d), exts, _depth + 1))
        if len(out) >= PHOTO_TREE_MAX:
            return out[:PHOTO_TREE_MAX]
    return out


def _photo_playlist(path):
    """(list of image paths, index of `path`).

    Browser order via `_folder_playlist`, or the whole subtree with the
    `photo.recursive` setting; falls back to the single clicked image."""
    folder = os.path.dirname(path) or "."
    exts = _photo_exts()
    try:
        recursive = bool(xbmc.getCondVisibility("Skin.HasSetting(photo.recursive)"))
    except Exception:
        recursive = False
    if recursive:
        plist = _photo_playlist_tree(folder, exts)
    else:
        plist = _folder_playlist(folder, exts)
    if path not in plist:
        base = sources.url_unquote(os.path.basename(path))
        for p in plist:
            if sources.url_unquote(os.path.basename(p)) == base:
                path = p
                break
    if path not in plist:
        plist = [path]
    return plist, plist.index(path)


# EXIF tag -> internal key (IFD0 + Exif sub-IFD).
_EXIF_TAGS = {
    0x010F: "make",
    0x0110: "model",
    0x0112: "orientation",
    0x0131: "software",
    0x0132: "datetime",
    0x829A: "exposure",
    0x829D: "fnumber",
    0x8822: "exposure_program",
    0x8827: "iso",
    0x9003: "datetime_original",
    0x9004: "datetime_digitized",
    0x9201: "shutter_speed",
    0x9202: "aperture",
    0x9207: "metering",
    0x9209: "flash",
    0x920A: "focal_length",
    0xA002: "pixel_x",
    0xA003: "pixel_y",
    0xA403: "white_balance",
    0xA405: "focal35",
    0xA434: "lens",
}


def _exif_parse(t):
    """Decode a raw TIFF/EXIF block into {key: value}; header-only, no decoding.
    Handles IFD0 and the Exif sub-IFD; rationals stay (num, den) tuples."""
    out = {}
    if len(t) < 8 or t[:2] not in (b"II", b"MM"):
        return out
    order = "little" if t[:2] == b"II" else "big"

    def u16(o):
        return int.from_bytes(t[o:o + 2], order)

    def u32(o):
        return int.from_bytes(t[o:o + 4], order)

    if u16(2) != 42:
        return out

    def value_of(typ, cnt, o):
        # Values are stored inline in the 4-byte field while they fit, else at
        # the u32 offset.
        if typ == 3:            # SHORT
            if cnt == 1:
                return u16(o + 8)
            if cnt == 2:
                return (u16(o + 8), u16(o + 10))  # some writers pair RATIONALs
            vo = u32(o + 8)
            return [u16(vo + 2 * k) for k in range(cnt) if vo + 2 * k + 2 <= len(t)]
        if typ == 4:            # LONG
            if cnt == 1:
                return u32(o + 8)
            vo = u32(o + 8)
            return [u32(vo + 4 * k) for k in range(cnt) if vo + 4 * k + 4 <= len(t)]
        if typ in (5, 10):      # RATIONAL / SRATIONAL
            def rat(off, signed):
                if off + 8 > len(t):
                    return None
                num = (int.from_bytes(t[off:off + 4], order, signed=signed))
                den = u32(off + 4)
                return (num, den)
            if cnt == 1:
                return rat(u32(o + 8), typ == 10)
            vo = u32(o + 8)
            return [rat(vo + 8 * k, typ == 10) for k in range(cnt)
                    if vo + 8 * k + 8 <= len(t)]
        if typ == 2:            # ASCII
            if cnt <= 4:
                raw = t[o + 8:o + 8 + cnt]
            else:
                vo = u32(o + 8)
                raw = t[vo:vo + cnt] if vo + cnt <= len(t) else b""
            return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        if typ in (1, 7):       # BYTE / UNDEFINED
            if cnt <= 4:
                return t[o + 8:o + 8 + cnt]
            vo = u32(o + 8)
            return t[vo:vo + cnt] if vo + cnt <= len(t) else b""
        if typ == 9:            # SLONG
            if cnt == 1:
                return int.from_bytes(t[o + 8:o + 12], order, signed=True)
            vo = u32(o + 8)
            return [int.from_bytes(t[vo + 4 * k:vo + 4 * k + 4], order, signed=True)
                    for k in range(cnt) if vo + 4 * k + 4 <= len(t)]
        return None

    def read_ifd(off, depth=0):
        if depth > 1 or off + 2 > len(t):
            return
        count = u16(off)
        o = off + 2
        for _ in range(count):
            if o + 12 > len(t):
                return
            tag = u16(o)
            typ = u16(o + 2)
            cnt = u32(o + 4)
            val = value_of(typ, cnt, o)
            if tag == 0x8769 and isinstance(val, int):
                read_ifd(val, depth + 1)
            elif tag in _EXIF_TAGS and val not in (None, "", []):
                out[_EXIF_TAGS[tag]] = val
            o += 12

    read_ifd(u32(4))
    return out


def _exif_locate(path):
    """Raw TIFF/EXIF block bytes from a JPEG/TIFF/HEIC image, or b"" (header-only)."""
    try:
        ext = os.path.splitext(path or "")[1].lower()
    except Exception:
        ext = ""
    data = _read_head(path, 524288 if ext in HEIC_EXT else 131072)
    if not data or len(data) < 8:
        return b""
    if data[0] == 0xFF and data[1] == 0xD8:
        i, n = 2, len(data)
        while i + 4 <= n:
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
                return data[i + 10:]
            i += 2 + (((data[i + 2] << 8) | data[i + 3]) if i + 3 < n else 0)
        return b""
    if ext in (".tif", ".tiff"):
        return data
    if ext in HEIC_EXT:
        # The Exif item carries raw TIFF data prefixed with "Exif\0\0".
        pos = data.find(b"Exif\x00\x00")
        return data[pos + 6:] if pos >= 0 else b""
    return b""


def _exif_orientation(path):
    """EXIF orientation (1-8) of a local or network image, else 1 (header-only)."""
    try:
        val = int(_exif_parse(_exif_locate(path)).get("orientation", 1))
    except (TypeError, ValueError):
        return 1
    return val if 1 <= val <= 8 else 1


def _exif_rat(r):
    """(num, den) -> float, or None."""
    try:
        num, den = r
        return (num / den) if den else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _exif_data(path):
    """Readable EXIF fields for the photo INFO modal (missing keys omitted)."""
    tags = _exif_parse(_exif_locate(path))
    out = {}
    for k in ("make", "model", "lens", "software"):
        v = tags.get(k)
        if v:
            out[k] = v
    dt = (tags.get("datetime_original") or tags.get("datetime_digitized")
          or tags.get("datetime") or "")
    if dt:
        # EXIF "YYYY:MM:DD HH:MM:SS" -> "YYYY-MM-DD HH:MM:SS".
        out["datetime"] = dt.replace(":", "-", 2)
    exp = _exif_rat(tags.get("exposure"))
    if exp and exp > 0:
        out["exposure"] = ("1/%d s" % round(1.0 / exp)) if exp < 1 else ("%.1f s" % exp)
    elif tags.get("shutter_speed") is not None:
        sv = _exif_rat(tags.get("shutter_speed"))
        if sv:
            out["exposure"] = "1/%d s" % round(2.0 ** sv)
    fn = _exif_rat(tags.get("fnumber"))
    if fn is None:
        av = _exif_rat(tags.get("aperture"))
        fn = (2.0 ** (av / 2.0)) if av else None
    if fn:
        out["fnumber"] = "f/%.1f" % fn
    iso = tags.get("iso")
    if iso:
        out["iso"] = "%s" % iso
    fl = _exif_rat(tags.get("focal_length"))
    if fl:
        out["focal"] = "%.0f mm" % fl
    # Resolution from the actual image header first; EXIF pixel dims can be stale.
    px = py = None
    try:
        px, py = _image_dims(path)
    except Exception:
        px, py = None, None
    if not (px and py):
        px, py = tags.get("pixel_x"), tags.get("pixel_y")
    if px and py:
        out["pixels"] = "%d\u00d7%d px" % (px, py)
    ori = tags.get("orientation")
    if ori:
        out["orientation"] = str(ori)
    return out


def _pil_import():
    """(Image, ImageOps, version) or (None, None, reason).

    A bare `from PIL import ...` can fail; also probe the script.module.pil/
    pillow addon libs, which Kodi does not always put on sys.path."""
    try:
        from PIL import Image, ImageOps
        try:
            ver = getattr(Image, "__version__", "?")
        except Exception:
            ver = "?"
        return Image, ImageOps, ver
    except Exception as e:
        first = e
    try:
        import xbmcaddon
    except Exception:
        return None, None, "no PIL (%s), no xbmcaddon" % first
    for mod in ("script.module.pil", "script.module.pillow"):
        try:
            base = xbmcaddon.Addon(mod).getAddonInfo("path")
        except Exception:
            continue
        for sub in ("lib", ""):
            cand = os.path.join(base, sub) if sub else base
            if cand not in sys.path and os.path.isdir(cand):
                sys.path.insert(0, cand)
        try:
            from PIL import Image, ImageOps
            try:
                ver = getattr(Image, "__version__", "?")
            except Exception:
                ver = "?"
            log("exif: PIL loaded via %s (%s)" % (mod, ver))
            return Image, ImageOps, ver
        except Exception:
            continue
    return None, None, "no PIL (%s)" % first


# ffmpeg video-filter per EXIF orientation (1 = normal).
_EXIF_FFMPEG_VF = {
    2: "hflip",
    3: "hflip,vflip",
    4: "vflip",
    5: "transpose=0",
    6: "transpose=1",
    7: "transpose=3",
    8: "transpose=2",
}


def _read_head(path, n=131072):
    """First n bytes of a local file or network (VFS) URL; b"" on error."""
    try:
        if sources.is_network_path(path):
            # Single network reader (metadata._open_bin); no local duplicate.
            from metadata import _open_bin
            with _open_bin(path) as f:
                return f.read(n)
        with open(path, "rb") as f:
            return f.read(n)
    except Exception:
        return b""


def _ffmpeg_bin():
    """ffmpeg binary path or None (rare on CoreELEC; macOS usually not)."""
    try:
        import shutil
        p = shutil.which("ffmpeg")
        if p:
            return p
    except Exception:
        pass
    for cand in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        try:
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        except Exception:
            continue
    return None


def _sips_bin():
    """macOS `sips` binary or None (built in since 10.4, decodes HEIC)."""
    cand = "/usr/bin/sips"
    try:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    except Exception:
        pass
    return None


def _decode_heic(src):
    """Decode a HEIC/HEIF file to an intermediate JPEG, or None. macOS `sips`
    first (it keeps the EXIF orientation tag), then ffmpeg."""
    sips = _sips_bin()
    if sips:
        import subprocess
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="heic-", suffix=".jpg")
        os.close(fd)
        try:
            r = subprocess.run(
                [sips, "-s", "format", "jpeg", src, "--out", tmp],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            if (r.returncode == 0 and os.path.isfile(tmp)
                    and os.path.getsize(tmp) > 0):
                return tmp
            err = (r.stderr or b"")[:200]
            try:
                err_s = err.decode("utf-8", "replace")
            except Exception:
                err_s = "?"
            log("heic: sips failed for %s (rc=%s %s)"
                % (redact(src), r.returncode, err_s))
        except Exception as e:
            log("heic: sips failed for %s: %s" % (redact(src), e))
        try:
            os.remove(tmp)
        except OSError:
            pass
    exe = _ffmpeg_bin()
    if exe:
        import subprocess
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="heic-", suffix=".jpg")
        os.close(fd)
        try:
            r = subprocess.run(
                [exe, "-y", "-v", "error", "-i", src, "-frames:v", "1", tmp],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            if (r.returncode == 0 and os.path.isfile(tmp)
                    and os.path.getsize(tmp) > 0):
                return tmp
        except Exception:
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass
    return None


def _oriented_photo_ffmpeg(path, orientation, out):
    """Rotate via ffmpeg (no Pillow needed). Returns True on success."""
    vf = _EXIF_FFMPEG_VF.get(orientation)
    if not vf:
        return False
    exe = _ffmpeg_bin()
    if not exe:
        return False
    try:
        import subprocess
        vf_full = ("%s,scale='min(iw,%d)':-2" % (vf, PHOTO_MAX_DIM))
        r = subprocess.run(
            [exe, "-y", "-v", "error", "-i", path, "-vf", vf_full,
             "-q:v", "2", out],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60)
        if r.returncode == 0 and os.path.isfile(out):
            return True
        err = (r.stderr or b"")[:200]
        try:
            err_s = err.decode("utf-8", "replace")
        except Exception:
            err_s = "?"
        log("exif: ffmpeg failed for %s (rc=%s %s)"
            % (redact(path), r.returncode, err_s))
        return False
    except Exception as e:
        log("exif: ffmpeg failed for %s: %s" % (redact(path), e))
        return False


def _download_vfs(path):
    """Download a network (VFS) URL to a temp local file, or None."""
    try:
        from metadata import _open_bin, _size
        try:
            total = _size(path)
        except Exception:
            total = 0
        if total and total > 64 * 1024 * 1024:
            log("exif: network file too large (%s bytes), skipped: %s"
                % (total, redact(path)))
            return None
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="exif-", suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as out:
                with _open_bin(path) as f:
                    while True:
                        chunk = f.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
            return tmp
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        log("exif: download failed for %s: %s" % (redact(path), e))
        return None


_SYS_PYTHON_SNIPPET = (
    "import sys;"
    "from PIL import Image, ImageOps;"
    "src, dst = sys.argv[1], sys.argv[2];"
    "im = Image.open(src);"
    "im = ImageOps.exif_transpose(im);"
    "im.thumbnail((%d, %d));"
    "im.convert('RGB').save(dst, 'JPEG', quality=90)"
) % (PHOTO_MAX_DIM, PHOTO_MAX_DIM)


def _sys_python():
    """System python3 binary or None (CoreELEC ships one with PIL)."""
    try:
        import shutil
        p = shutil.which("python3")
        if p:
            return p
    except Exception:
        pass
    for cand in ("/usr/bin/python3", "/usr/local/bin/python3"):
        try:
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        except Exception:
            continue
    return None


def _oriented_photo_syspython(src, out):
    """Rotate via the system python3 + PIL (CoreELEC ships Pillow there); True
    on success."""
    exe = _sys_python()
    if not exe:
        return False
    try:
        import subprocess
        r = subprocess.run(
            [exe, "-c", _SYS_PYTHON_SNIPPET, src, out],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=90)
        if r.returncode == 0 and os.path.isfile(out):
            return True
        err = (r.stderr or b"")[:200]
        try:
            err_s = err.decode("utf-8", "replace")
        except Exception:
            err_s = "?"
        log("exif: system python failed (rc=%s %s)" % (r.returncode, err_s))
        return False
    except Exception as e:
        log("exif: system python failed: %s" % e)
        return False


def _photo_cache_ok(out):
    """True when a cached JPEG is complete (starts SOI FFD8, ends EOI FFD9)."""
    try:
        if not os.path.isfile(out) or os.path.getsize(out) < 4:
            return False
        with open(out, "rb") as f:
            head = f.read(2)
            f.seek(-2, os.SEEK_END)
            tail = f.read(2)
        return head == b"\xff\xd8" and tail == b"\xff\xd9"
    except Exception:
        return False


def _oriented_photo(path):
    """Display path with the EXIF orientation applied (cached).

    Kodi's GUI image control does NOT rotate EXIF JPEGs, so a rotated copy is
    rendered once. Backend cascade: Pillow, system python3+PIL, ffmpeg.
    Returns None when nothing can decode the file (typical HEIC), so the
    viewer never shows a black frame."""
    ori = _exif_orientation(path)
    try:
        is_heic = os.path.splitext(path or "")[1].lower() in HEIC_EXT
    except Exception:
        is_heic = False
    if ori in (1, 0) and not is_heic:
        return path
    is_net = sources.is_network_path(path)
    try:
        if is_net:
            try:
                from metadata import _size
                size = _size(path)
            except Exception:
                size = 0
            key = hashlib.md5(("net|%s|%d|%s|%d" % (cache_key(path), size,
                                                     PHOTO_ORIENT_TAG, PHOTO_MAX_DIM))
                              .encode("utf-8", "surrogateescape")).hexdigest()
        else:
            st = os.stat(path)
            key = hashlib.md5(("%s|%d|%d|%s|%d" % (path, st.st_mtime, st.st_size,
                                                   PHOTO_ORIENT_TAG, PHOTO_MAX_DIM))
                              .encode("utf-8", "surrogateescape")).hexdigest()
        out_dir = os.path.join(state_dir(), "photo-orient")
        out = os.path.join(out_dir, key + ".jpg")
        # A partial cache file must not be served forever (its mtime is newer).
        if _photo_cache_ok(out):
            if is_net or os.path.getmtime(out) >= st.st_mtime:
                return out
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:
        log("exif: orientation failed for %s: %s" % (redact(path), e))
        return path if not is_heic else None
    src = path
    tmp = None
    heic_tmp = None
    if is_net:
        tmp = _download_vfs(path)
        if not tmp:
            return path if not is_heic else None
        src = tmp
    try:
        if is_heic:
            heic_tmp = _decode_heic(src)
            if heic_tmp:
                src = heic_tmp
        Image, ImageOps, pil_info = _pil_import()
        if Image is not None:
            try:
                im = Image.open(src)
                # No im.draft(): it would cap the result below the source
                # resolution even when the source is under PHOTO_MAX_DIM.
                im = ImageOps.exif_transpose(im)
                im.thumbnail((PHOTO_MAX_DIM, PHOTO_MAX_DIM))
                im.convert("RGB").save(out, "JPEG", quality=90)
                if _photo_cache_ok(out):
                    log("exif: oriented %s (tag %s) via Pillow %s"
                        % (redact(path), ori, pil_info))
                    return out
                raise IOError("Pillow wrote an incomplete JPEG")
            except Exception as e:
                log("exif: Pillow failed for %s (tag %s, Pillow %s): %s"
                    % (redact(path), ori, pil_info, e))
                try:
                    os.remove(out)
                except OSError:
                    pass
        else:
            try:
                py = "%s.%s.%s" % (sys.version_info[0], sys.version_info[1],
                                   sys.version_info[2])
            except Exception:
                py = "?"
            log("exif: %s (tag %s, python %s), trying system python"
                % (pil_info, ori, py))
        if _oriented_photo_syspython(src, out) and _photo_cache_ok(out):
            log("exif: oriented %s (tag %s) via system python"
                % (redact(path), ori))
            return out
        if _oriented_photo_ffmpeg(src, ori, out) and _photo_cache_ok(out):
            log("exif: oriented %s (tag %s) via ffmpeg"
                % (redact(path), ori))
            return out
        if is_heic and heic_tmp and os.path.isfile(heic_tmp):
            # Decoded but no rotation backend: serve the JPEG as-is (Kodi 21
            # cannot display the HEIC at all).
            try:
                shutil.copyfile(heic_tmp, out)
                if _photo_cache_ok(out):
                    log("heic: decoded %s (no rotation backend)" % redact(path))
                    return out
            except Exception:
                pass
        if is_heic:
            # No decoder: skip instead of a black frame or wrong native decode.
            log("heic: undecodable, skipped: %s" % redact(path))
            return None
        log("exif: orientation skipped for %s (tag %s, no backend)"
            % (redact(path), ori))
        return path
    finally:
        for extra in (heic_tmp, tmp):
            if extra:
                try:
                    os.remove(extra)
                except OSError:
                    pass


def _image_dims(path):
    """(width, height) of an image, or (None, None). Pillow for local files,
    else a header parse of PNG/JPEG/GIF/BMP (also for network URLs)."""
    try:
        if not sources.is_network_path(path):
            Image, _ImageOps, _ver = _pil_import()
            if Image is not None:
                with Image.open(path) as im:
                    return int(im.size[0]), int(im.size[1])
    except Exception:
        pass
    head = _read_head(path, 131072)
    if not head or len(head) < 10:
        return None, None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        w = int.from_bytes(head[16:20], "big")
        h = int.from_bytes(head[20:24], "big")
        if w > 0 and h > 0:
            return w, h
    if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        w = int.from_bytes(head[6:8], "little")
        h = int.from_bytes(head[8:10], "little")
        if w > 0 and h > 0:
            return w, h
    if head[:2] == b"\xff\xd8":
        i, n = 2, len(head)
        # SOF0..SOF15 except the non-frame markers DHT(0xC4)/JPG(0xC8)/DAC(0xCC).
        while i + 9 < n:
            if head[i] != 0xFF:
                break
            m = head[i + 1]
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(head[i + 5:i + 7], "big")
                w = int.from_bytes(head[i + 7:i + 9], "big")
                if w > 0 and h > 0:
                    return w, h
                break
            if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            ln = int.from_bytes(head[i + 2:i + 4], "big")
            if ln < 2:
                break
            i += 2 + ln
    if head[:2] == b"BM" and len(head) >= 26:
        w = int.from_bytes(head[18:22], "little", signed=True)
        h = int.from_bytes(head[22:26], "little", signed=True)
        if w > 0 and h:
            return w, abs(h)
    return None, None


_KB_SYS_PYTHON_SNIPPET = (
    "import sys\n"
    "from PIL import Image\n"
    "src, dst, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
    "im = Image.open(src)\n"
    "if max(im.size) <= cap:\n"
    "    sys.exit(0)\n"
    "im.draft('RGB', (cap, cap))\n"
    "im.thumbnail((cap, cap))\n"
    "im.convert('RGB').save(dst, 'JPEG', quality=90)\n"
    "sys.exit(4)\n"
)


def _kb_scale_file(src, out, cap):
    """Downscale local src to cap into out. True when out is a NEW copy, False
    when src already fits. Pillow, then system python3, then ffmpeg."""
    try:
        w, h = _image_dims(src)
    except Exception:
        w, h = None, None
    if w and h and max(w, h) <= cap:
        return False
    try:
        Image, _ImageOps, _ver = _pil_import()
        if Image is not None:
            with Image.open(src) as im:
                if max(im.size) <= cap:
                    return False
                # Decode at the smallest JPEG scale covering the target:
                # cuts peak RAM ~4-16x with an identical result.
                im.draft("RGB", (cap, cap))
                im.thumbnail((cap, cap))
                im.convert("RGB").save(out, "JPEG", quality=90)
            if _photo_cache_ok(out):
                log("kb: scaled %s via Pillow" % redact(src))
                return True
    except Exception as e:
        log("kb: Pillow failed for %s: %s" % (redact(src), e))
        try:
            os.remove(out)
        except OSError:
            pass
    exe = _sys_python()
    proven_small = False
    if exe:
        try:
            import subprocess
            r = subprocess.run(
                [exe, "-c", _KB_SYS_PYTHON_SNIPPET, src, out, str(cap)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=90)
            if r.returncode == 4 and _photo_cache_ok(out):
                log("kb: scaled %s via system python" % redact(src))
                return True
            if r.returncode == 0:
                proven_small = True
            else:
                err = ((r.stderr or b"")[:200]).decode("utf-8", "replace")
                log("kb: system python failed (rc=%s %s)" % (r.returncode, err))
        except Exception as e:
            log("kb: system python failed: %s" % e)
    if not proven_small and (w is None or (w and max(w, h) > cap)):
        # Last resort without Pillow: only for large files.
        try:
            big = os.path.getsize(src) > 2 * 1024 * 1024
        except OSError:
            big = False
        exe = _ffmpeg_bin()
        if big and exe:
            try:
                import subprocess
                r = subprocess.run(
                    [exe, "-y", "-v", "error", "-i", src, "-vf",
                     "scale='min(iw,%d)':-2" % cap, "-q:v", "2", out],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=90)
                if r.returncode == 0 and _photo_cache_ok(out):
                    log("kb: scaled %s via ffmpeg" % redact(src))
                    return True
            except Exception as e:
                log("kb: ffmpeg failed for %s: %s" % (redact(src), e))
    try:
        if _photo_cache_ok(out):
            return True
        os.remove(out)
    except OSError:
        pass
    return False


def _mem_total_mb():
    """Total RAM in MB from /proc/meminfo, or 0 when unknown (non-Linux)."""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _kb_cap():
    """Ken Burns texture cap for the current display width.

    Zoom peaks near 118%, so cap = display * 1.18 rounded up to 256, clamped
    to [2560, 4096]. Low-RAM devices (<1800 MB) stay at the 2560 floor: a 4K
    texture is ~44 MB and OOM-killed 1 GB boxes."""
    disp = 1920
    try:
        head = (xbmc.getInfoLabel("System.ScreenResolution") or "").split()
        w = int(head[0].split("x")[0]) if head else 0
        if w > 0:
            disp = w
    except Exception:
        pass
    cap = max(PHOTO_KB_DIM, min(4096, (int(disp * 1.18) + 255) // 256 * 256))
    total = _mem_total_mb()
    if total and total < 1800:
        return PHOTO_KB_DIM
    return cap


def _kb_photo(path):
    """Ken Burns display path: `_oriented_photo` result capped to display width
    via a cached downscale; falls back to the uncapped base on failure."""
    base = _oriented_photo(path)
    if base is None:
        return None
    cap = _kb_cap()
    try:
        is_net = sources.is_network_path(base)
    except Exception:
        is_net = False
    tmp = None
    try:
        if is_net:
            try:
                from metadata import _size
                size = _size(base)
            except Exception:
                size = 0
            if size and size <= 3 * 1024 * 1024:
                return base
            src = None
            key = hashlib.md5(("kb|net|%s|%d|%d"
                               % (cache_key(base), size, cap))
                              .encode("utf-8", "surrogateescape")).hexdigest()
        else:
            try:
                st = os.stat(base)
            except OSError:
                return base
            src = base
            key = hashlib.md5(("kb|%s|%d|%d|%d"
                               % (base, st.st_mtime, st.st_size, cap))
                              .encode("utf-8", "surrogateescape")).hexdigest()
        out = os.path.join(state_dir(), "photo-scale", key + ".jpg")
        # Cache FIRST: the key needs no file content, so a background-preloaded
        # copy is reused instead of re-downloading every step.
        if _photo_cache_ok(out):
            return out
        if is_net:
            tmp = _download_vfs(base)
            if not tmp:
                return base
            src = tmp
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
        except OSError:
            return base
        if _kb_scale_file(src, out, cap) or _photo_cache_ok(out):
            return out
        return base
    except Exception as e:
        log("kb: scale failed for %s: %s" % (redact(path), e))
        return base
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _still_photo(path):
    """Display path for the still modes (standard / fill): full-resolution
    original, or the same RAM gate as Ken Burns on low-RAM devices."""
    total = _mem_total_mb()
    if total and total < 1800:
        return _kb_photo(path)
    return _oriented_photo(path)


def _first_displayable(plist, idx):
    """Index of the first displayable image at/after idx (wrapping), or -1."""
    n = len(plist)
    for k in range(n):
        i = (idx + k) % n
        if _oriented_photo(plist[i]) is not None:
            return i
    return -1


def _photo_prepare(path):
    """Warm the display cache for path in the current mode."""
    if _photo_mode() == PHOTO_KB_MODE:
        return _kb_photo(path)
    return _still_photo(path)


def _photo_next_index(idx, n):
    """Next slideshow index (no I/O); None at the end with repeat off."""
    if n <= 1:
        return None
    if _photo_shuffle() == 1:
        return random.choice([i for i in range(n) if i != idx])
    if idx + 1 < n:
        return idx + 1
    return 0 if _photo_repeat() == 1 else None


def _photo_preload(win, plist, idx):
    """Prepare the next slideshow image in the background.

    `bp.photo.preidx` is published only once the copy is ready. Only the
    long-lived home daemon preloads (a manual step's worker dies with its
    short-lived interpreter). ONE download at a time; while one is in flight
    the next step falls back to the normal path."""
    if not win.getProperty("bp.home-daemon.running"):
        return
    if win.getProperty("bp.photo.preloading") == "1":
        win.clearProperty("bp.photo.preidx")
        return
    nxt = _photo_next_index(idx, len(plist))
    if nxt is None or nxt == idx:
        win.clearProperty("bp.photo.preidx")
        return
    # Not ready yet: a step must not pick it up before the copy exists.
    win.clearProperty("bp.photo.preidx")
    path = plist[nxt]
    gen = win.getProperty("bp.photo.gen") or ""
    win.setProperty("bp.photo.preloading", "1")

    def _work():
        ok = False
        try:
            ok = _photo_prepare(path) is not None
        except Exception as e:
            log("photo preload failed for %s: %s" % (redact(path), e))
        finally:
            win.clearProperty("bp.photo.preloading")
        # Publish into this viewer session only (gen guard).
        if (ok and win.getProperty("bp.photo") == "open"
                and (win.getProperty("bp.photo.gen") or "") == gen):
            win.setProperty("bp.photo.preidx", str(nxt))

    try:
        import threading
        threading.Thread(target=_work, daemon=True).start()
    except Exception:
        win.clearProperty("bp.photo.preloading")


def photo_keep_preloaded():
    """Start one preload for the running slideshow (home daemon duty); no-op
    while a copy is ready or in flight."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") != "open":
        return
    if win.getProperty("bp.photo.playing") != "1":
        return
    if win.getProperty("bp.photo.preidx"):
        return
    if win.getProperty("bp.photo.preloading") == "1":
        return
    try:
        plist = [path_dec(x) for x in
                 json.loads(win.getProperty("bp.photo.list") or "[]")]
        idx = int(win.getProperty("bp.photo.idx") or "0")
    except Exception:
        return
    if plist:
        _photo_preload(win, plist, idx)


def _photo_show(win, plist, idx):
    """Point the viewer at plist[idx]: image + OSD lines. Returns False when the
    entry cannot be displayed. Ken Burns serves the capped _kb_photo copy."""
    p = plist[idx]
    if _photo_mode() == PHOTO_KB_MODE:
        disp_path = _kb_photo(p)
    else:
        disp_path = _still_photo(p)
    if disp_path is None:
        log("photo: skip undecodable %s" % redact(p))
        return False
    win.setProperty("bp.photo.idx", str(idx))
    win.setProperty("bp.photo.count", str(len(plist)))
    win.setProperty("bp.photo.path", play_str(disp_path))
    disp = sources.url_display(p)
    name = safe_label(os.path.basename(disp))
    below = sources.display_path_below_source(os.path.dirname(disp) or ".")
    leaf = safe_label(os.path.basename((os.path.dirname(disp) or ".").rstrip("/")) or "")
    if leaf in ("", ".", "/"):
        leaf = below
    win.setProperty("bp.photo.title", name)
    win.setProperty("bp.photo.title.rep", _ticker(name, 20))
    win.setProperty("bp.photo.pathdisp", below)
    win.setProperty("bp.photo.pathdisp.rep", _ticker(below, 11))
    win.setProperty("bp.photo.folder", leaf)
    win.setProperty("bp.photo.folder.rep", _ticker(leaf, 11))
    # Chain the preload; drop the stale index FIRST so a later step cannot
    # pick it up.
    win.clearProperty("bp.photo.preidx")
    if win.getProperty("bp.photo.playing") == "1":
        _photo_preload(win, plist, idx)
    return True


def _photo_interval():
    """Current slideshow interval in seconds (window property, fallback 5)."""
    try:
        iv = int(float(xbmcgui.Window(10000).getProperty("bp.photo.interval")
                       or PHOTO_INTERVAL))
    except (TypeError, ValueError):
        iv = PHOTO_INTERVAL
    return iv if iv in PHOTO_INTERVALS else PHOTO_INTERVAL


def _photo_interval_icon(iv):
    """Glyph path for an interval value (falls back to the default 5 s)."""
    return PHOTO_INTERVAL_ICONS.get(iv, PHOTO_INTERVAL_ICONS[PHOTO_INTERVAL])


# Photo session scratch caches (derived data): cleared on viewer open and close.
_PHOTO_CACHE_DIRS = ("photo-orient", "photo-scale")
_PHOTO_TMP_PATTERNS = ("exif-*.jpg", "heic-*.jpg")


def _photo_cache_clear():
    """Delete the photo caches and our own temp downloads (only our own files)."""
    try:
        import glob
        import tempfile
    except Exception:
        return
    for sub in _PHOTO_CACHE_DIRS:
        d = os.path.join(state_dir(), sub)
        try:
            for name in os.listdir(d):
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass
        except OSError:
            pass
    try:
        tmpdir = tempfile.gettempdir()
        for pat in _PHOTO_TMP_PATTERNS:
            for path in glob.glob(os.path.join(tmpdir, pat)):
                try:
                    os.remove(path)
                except OSError:
                    pass
    except Exception as e:
        log("photo: cache clear (temps) failed: %s" % e)


def photo_open(path):
    """Open the fullscreen photo viewer + slideshow (own window 1151).

    The core picture viewer always flashes black, so the image is shown in
    our own window. The folder's images become a playlist so Previous/Next
    and the slideshow walk the folder. `path` may be local or a VFS URL."""
    _photo_cache_clear()
    win = xbmcgui.Window(10000)
    # Viewing-session stamp so a previous session's preload cannot publish.
    win.setProperty("bp.photo.gen", repr(time.time()))
    win.clearProperty("bp.photo.preloading")
    # Spinner for the initial download/scale; cleared in the finally.
    win.setProperty("bp.photo.loading", "1")
    try:
        _photo_open_ui(win, path)
    finally:
        win.clearProperty("bp.photo.loading")


def _photo_open_ui(win, path):
    """photo_open body (spinner set by the caller)."""
    plist, idx = _photo_playlist(path)
    idx = _first_displayable(plist, idx)
    if idx < 0:
        log("photo: nothing displayable in %s" % redact(path))
        return
    win.setProperty("bp.photo.list", json.dumps([path_enc(p) for p in plist]))
    win.clearProperty("bp.photo.playing")
    win.clearProperty("bp.photo.next")
    # Interval + mode from the persisted skin strings; set before opening so
    # the first frame is right.
    try:
        iv = int(xbmc.getInfoLabel("Skin.String(photo.interval)") or PHOTO_INTERVAL)
    except (TypeError, ValueError):
        iv = PHOTO_INTERVAL
    if iv not in PHOTO_INTERVALS:
        iv = PHOTO_INTERVAL
    win.setProperty("bp.photo.interval", str(iv))
    win.setProperty("bp.photo.interval.icon", _photo_interval_icon(iv))
    try:
        pm = int(xbmc.getInfoLabel("Skin.String(photo.mode)") or 0)
    except (TypeError, ValueError):
        pm = 0
    win.setProperty("bp.photo.mode", str(pm if pm in PHOTO_MODES else 0))
    if (pm if pm in PHOTO_MODES else 0) == PHOTO_KB_MODE:
        _photo_kb_seed(win)
    # Repeat (default on) + shuffle (default off) from skin strings; set
    # before opening so the OSD button icons are right on the first frame.
    try:
        rep = int(xbmc.getInfoLabel("Skin.String(photo.repeat)") or 1)
    except (TypeError, ValueError):
        rep = 1
    win.setProperty("bp.photo.repeat", "0" if rep == 0 else "1")
    try:
        shf = int(xbmc.getInfoLabel("Skin.String(photo.shuffle)") or 0)
    except (TypeError, ValueError):
        shf = 0
    win.setProperty("bp.photo.shuffle", "1" if shf == 1 else "0")
    # Open with an EMPTY texture first: the control keeps its last texture
    # across close/open, so the previous picture would flash otherwise.
    win.setProperty("bp.photo.path", "")
    win.setProperty("bp.photo", "open")
    try:
        xbmc.executebuiltin("ActivateWindow(1151)")
    except Exception:
        pass
    for _ in range(20):
        try:
            if xbmc.getCondVisibility("Window.IsActive(1151)"):
                break
        except Exception:
            break
        time.sleep(0.1)
    # Keep the screen awake (opt-out via `dim.screen`); released in photo_close.
    try:
        if not xbmc.getCondVisibility("Skin.HasSetting(dim.screen)"):
            xbmc.executebuiltin("InhibitScreensaver(true)")
    except Exception:
        pass
    # Reset the progress slider (a stale skin string could linger).
    try:
        xbmc.executebuiltin("Skin.SetString(photo.progress,0)")
    except Exception:
        pass
    # OSD starts visible; the home daemon hides/reveals it.
    win.setProperty("bp.photo.osd", "1")
    win.setProperty("bp.photo.osd.focus", "901")
    time.sleep(0.08)
    _photo_show(win, plist, idx)
    # Spinner is for the OPEN only; slideshow steps need none.
    win.clearProperty("bp.photo.loading")
    for _ in range(10):
        xbmc.executebuiltin("SetFocus(901)")
        time.sleep(0.06)
        if xbmc.getCondVisibility("Control.HasFocus(901)"):
            break
    log("photo open: %s (%d/%d)" % (redact(path), idx + 1, len(plist)))


def photo_step(delta):
    """Previous (-1) / next (+1) image in the folder playlist.

    Repeat wraps at the ends, repeat off stops; shuffle only affects Next."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") != "open":
        return
    try:
        plist = [path_dec(x) for x in json.loads(win.getProperty("bp.photo.list") or "[]")]
    except Exception:
        return
    if not plist:
        return
    try:
        idx = int(win.getProperty("bp.photo.idx") or "0")
    except ValueError:
        idx = 0
    n = len(plist)
    if delta > 0:
        # A ready preload is already downloaded/scaled: swap instantly.
        try:
            pre = int(win.getProperty("bp.photo.preidx") or "-1")
        except (TypeError, ValueError):
            pre = -1
        ok_pre = 0 <= pre < n and (pre != idx or n == 1)
        if ok_pre and _photo_shuffle() != 1:
            # Sequential: the preload target must still be the next index.
            nxt = idx + 1
            if nxt >= n:
                nxt = 0 if _photo_repeat() == 1 else -1
            ok_pre = (nxt == pre)
        if ok_pre and _photo_show(win, plist, pre):
            win.clearProperty("bp.photo.preidx")
            if win.getProperty("bp.photo.playing") == "1":
                win.setProperty("bp.photo.next",
                                repr(time.time() + _photo_interval()))
            log("photo step +1 -> %d/%d (preloaded)" % (pre + 1, n))
            return
    if delta > 0 and _photo_shuffle() == 1 and n > 1:
        order = [i for i in range(n) if i != idx]
        random.shuffle(order)
        for i in order:
            if not _photo_show(win, plist, i):
                continue
            if win.getProperty("bp.photo.playing") == "1":
                win.setProperty("bp.photo.next", repr(time.time() + _photo_interval()))
            log("photo step %+d -> %d/%d (shuffled)" % (delta, i + 1, n))
            return
        log("photo step %+d: no displayable image" % delta)
        return
    loop = _photo_repeat() == 1
    for k in range(1, n + 1):
        i = idx + delta * k
        if loop:
            i %= n
        elif i < 0 or i >= n:
            break
        if not _photo_show(win, plist, i):
            continue
        if win.getProperty("bp.photo.playing") == "1":
            win.setProperty("bp.photo.next", repr(time.time() + _photo_interval()))
        log("photo step %+d -> %d/%d" % (delta, i + 1, n))
        return
    if delta > 0 and win.getProperty("bp.photo.playing") == "1":
        win.clearProperty("bp.photo.playing")
        win.clearProperty("bp.photo.next")
        log("photo: end of slideshow (repeat off)")
        return
    log("photo step %+d: at end (repeat off)" % delta)


def photo_play():
    """Toggle the photo slideshow (auto-advance every photos interval)."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") != "open":
        return
    if win.getProperty("bp.photo.playing") == "1":
        win.clearProperty("bp.photo.playing")
        win.clearProperty("bp.photo.next")
        log("photo: pause")
    else:
        iv = _photo_interval()
        win.setProperty("bp.photo.playing", "1")
        win.setProperty("bp.photo.next", repr(time.time() + iv))
        # The home daemon starts the first preload (this interpreter exits
        # right away).
        log("photo: play (%ds)" % iv)


def photo_interval_cycle():
    """Cycle the slideshow interval (OSD button) and persist it."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") != "open":
        return
    cur = _photo_interval()
    nxt = PHOTO_INTERVALS[(PHOTO_INTERVALS.index(cur) + 1) % len(PHOTO_INTERVALS)]
    win.setProperty("bp.photo.interval", str(nxt))
    win.setProperty("bp.photo.interval.icon", _photo_interval_icon(nxt))
    try:
        xbmc.executebuiltin("Skin.SetString(photo.interval,%d)" % nxt)
    except Exception:
        pass
    # Restart the running countdown so the new duration applies immediately.
    if win.getProperty("bp.photo.playing") == "1":
        win.setProperty("bp.photo.next", repr(time.time() + nxt))
    log("photo interval -> %ds" % nxt)


# Display modes: 0 = fit, 1 = fill, 2 = Ken Burns. Persisted in "photo.mode".
PHOTO_MODES = (0, 1, 2)


def _photo_kb_seed(win):
    """Pick a random Ken Burns corner variant and stamp its start time."""
    try:
        win.setProperty("bp.photo.kb", str(random.randrange(PHOTO_KB_CORNERS)))
        win.setProperty("bp.photo.kb.t", repr(time.time()))
    except Exception:
        pass


def photo_kb_tick():
    """Advance the Ken Burns corner once per cycle; at the boundary every
    variant sits at the image centre, so the flip is seamless."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") != "open":
        return
    try:
        mode = int(win.getProperty("bp.photo.mode") or "-1")
    except (TypeError, ValueError):
        mode = -1
    if mode != PHOTO_KB_MODE:
        return
    try:
        t0 = float(win.getProperty("bp.photo.kb.t") or "0")
    except (TypeError, ValueError):
        t0 = 0.0
    now = time.time()
    if t0 and now - t0 < PHOTO_KB_CYCLE:
        return
    try:
        cur = int(win.getProperty("bp.photo.kb") or "-1")
    except (TypeError, ValueError):
        cur = -1
    nxt = random.randrange(PHOTO_KB_CORNERS)
    if nxt == cur:
        nxt = (nxt + 1) % PHOTO_KB_CORNERS
    win.setProperty("bp.photo.kb", str(nxt))
    win.setProperty("bp.photo.kb.t", repr(now))
    log("photo: ken burns corner -> %d" % nxt)


def _photo_mode():
    try:
        m = int(float(xbmcgui.Window(10000).getProperty("bp.photo.mode") or 0))
    except (TypeError, ValueError):
        m = 0
    return m if m in PHOTO_MODES else 0


def photo_mode_cycle():
    """Cycle standard -> fill -> Ken Burns (OSD button); persisted."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") != "open":
        return
    m = (_photo_mode() + 1) % len(PHOTO_MODES)
    win.setProperty("bp.photo.mode", str(m))
    if m == PHOTO_KB_MODE:
        _photo_kb_seed(win)
    try:
        xbmc.executebuiltin("Skin.SetString(photo.mode,%d)" % m)
    except Exception:
        pass
    # Re-point the current image so the mode switch takes effect now.
    try:
        plist = [path_dec(x) for x in json.loads(win.getProperty("bp.photo.list") or "[]")]
        idx = int(win.getProperty("bp.photo.idx") or "0")
        if plist and 0 <= idx < len(plist):
            _photo_show(win, plist, idx)
    except Exception:
        pass
    log("photo mode -> %d" % m)


# Repeat/shuffle toggles, persisted in "photo.repeat"/"photo.shuffle".


def _photo_repeat():
    """Loop toggle: 1 = wrap at the ends (default), 0 = stop at the ends."""
    return 0 if xbmcgui.Window(10000).getProperty("bp.photo.repeat") == "0" else 1


def _photo_shuffle():
    """Shuffle toggle: 1 = Next picks a random image, 0 = sequential."""
    return 1 if xbmcgui.Window(10000).getProperty("bp.photo.shuffle") == "1" else 0


def photo_repeat_toggle():
    """Toggle the slideshow loop (OSD button); persisted."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") != "open":
        return
    nxt = 0 if _photo_repeat() == 1 else 1
    win.setProperty("bp.photo.repeat", str(nxt))
    try:
        xbmc.executebuiltin("Skin.SetString(photo.repeat,%d)" % nxt)
    except Exception:
        pass
    log("photo repeat -> %d" % nxt)


def photo_shuffle_toggle():
    """Toggle random Next (OSD button); persisted."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") != "open":
        return
    nxt = 0 if _photo_shuffle() == 1 else 1
    win.setProperty("bp.photo.shuffle", str(nxt))
    try:
        xbmc.executebuiltin("Skin.SetString(photo.shuffle,%d)" % nxt)
    except Exception:
        pass
    log("photo shuffle -> %d" % nxt)


def photo_show_osd():
    """Reveal the photo OSD (backdrop click / OK while it is hidden)."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") != "open":
        return
    win.setProperty("bp.photo.osd", "1")
    xbmc.executebuiltin(
        "SetFocus(%s)" % (win.getProperty("bp.photo.osd.focus") or "901"))


def photo_close():
    """Close the photo viewer window and put focus back on the list."""
    win = xbmcgui.Window(10000)
    _photo_cache_clear()
    # Release the screensaver inhibit taken in photo_open.
    try:
        xbmc.executebuiltin("InhibitScreensaver(false)")
    except Exception:
        pass
    # A still-open INFO modal (photo EXIF) must not linger over the list.
    win.clearProperty("bp.info")
    win.clearProperty("bp.info.from")
    for prop in ("bp.photo", "bp.photo.loading", "bp.photo.preidx",
                   "bp.photo.preloading", "bp.photo.gen",
                   "bp.photo.path", "bp.photo.list", "bp.photo.idx",
                   "bp.photo.count", "bp.photo.title", "bp.photo.title.rep",
                   "bp.photo.pathdisp", "bp.photo.pathdisp.rep",
                   "bp.photo.folder", "bp.photo.folder.rep",
                   "bp.photo.playing", "bp.photo.next",
                   "bp.photo.interval", "bp.photo.interval.icon",
                   "bp.photo.mode", "bp.photo.repeat", "bp.photo.shuffle",
                   "bp.photo.kb", "bp.photo.kb.t",
                   "bp.photo.osd", "bp.photo.osd.focus"):
        win.clearProperty(prop)
    try:
        xbmc.executebuiltin("ActivateWindow(Home)")
    except Exception:
        pass
    # Give the window switch a frame before taking focus back.
    for _ in range(6):
        time.sleep(0.06)
        xbmc.executebuiltin("SetFocus(33)")
        if xbmc.getCondVisibility("Control.HasFocus(33)"):
            break


def _stop_video_if_any():
    """Stop any background video before starting new media: a leftover video
    behind Home crashed Kodi when the next one started."""
    try:
        if xbmc.getCondVisibility("Player.HasVideo"):
            xbmc.Player().stop()
    except Exception:
        pass


def _seek_after_start(seconds):
    """Seek to `seconds` once the player is actually running (bounded)."""
    p = xbmc.Player()
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            if p.isPlaying() and p.getTotalTime() > 0:
                break
        except Exception:
            pass
        time.sleep(0.1)
    try:
        p.seekTime(float(seconds))
        log("resume: seek to %.0fs" % float(seconds))
    except Exception as e:
        log("resume: seek failed: %s" % e)


def _resume_prompt(path, entry):
    """Continue-watching modal; the buttons run main.py resumeyes/resumeno."""
    win = xbmcgui.Window(10000)
    win.setProperty("bp.resume.path", path_enc(path))
    win.setProperty("bp.resume.t", str(entry.get("t", 0)))
    win.setProperty("bp.resume.line", L(31528) % resume.fmt(entry.get("t", 0)))
    win.setProperty("bp.resume", "open")
    time.sleep(0.4)
    xbmc.executebuiltin("SetFocus(906)")
    log("resume prompt: %s @%s" % (redact(path), resume.fmt(entry.get("t", 0))))


def open_file(path, resume_pos=None):
    path = (path or "").strip()
    if not path:
        return
    path = fs_path(path)
    if not os.path.exists(path):
        from navigation import resolve_real  # lazy, mirrors nav()
        path = resolve_real(path)
    if os.path.isdir(path):
        nav(path)
        return
    ext = os.path.splitext(path)[1].lower()
    if ext in PLAYABLE_EXT:
        if resume_pos is None:
            entry = resume.get(path)
            if resume.promptable(entry):
                _resume_prompt(path, entry)
                return
        if ext in AUDIO_EXT:
            # Network audio: raise bp.aload BEFORE the core blocks the main
            # thread (measured ~10 s on WebDAV); the home daemon clears it.
            net = sources.is_network_path(path)
            if net:
                win = xbmcgui.Window(10000)
                win.setProperty("bp.aload", "1")
                win.setProperty("bp.aload.t", repr(time.time()))
            _stop_video_if_any()
            _play_audio_folder(path)
            if net:
                time.sleep(0.5)
            if resume_pos:
                _seek_after_start(resume_pos)
            _focus_play_button()
        else:
            _stop_video_if_any()
            _play_video_folder(path)
            if resume_pos:
                _seek_after_start(resume_pos)
            _focus_video_button()
    elif ext in IMAGE_EXT:
        # HEIC on a platform without a decoder stays inert instead of a black frame.
        if ext in HEIC_EXT and not heic_capable():
            log("open: HEIC skipped (no decoder): %s" % redact(path))
            return
        # Own fullscreen overlay instead of the core picture viewer (black flash).
        photo_open(path)
    elif ext in ARCHIVE_EXT:
        # Archives stay inert files (core flags them IsFolder, but we show a
        # file icon).
        log("open: archives parked for %s" % redact(path))
        return
    else:
        log("open: no handler yet for %s" % redact(path))

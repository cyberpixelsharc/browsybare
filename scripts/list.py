#!/usr/bin/env python3
"""List source for the file manager lists (plugin://browsybare/list).

q (search) and h (show hidden) arrive via the content URL params."""
import datetime
import json
import os
import sys
import time
from urllib.parse import quote, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blacklist
import search
import sources
from blacklist import blocked
from navigation import drive_root_of
from sources import is_network_path
from common import safe_label, redact, path_dec, natkey, write_json, state_file, cache_key, heic_capable, listing_get, listing_put, log as _common_log

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs

FOLDER_CACHE = "folder-sizes.json"


def log(msg):
    # Sanitized sink: raw paths may carry surrogates the logging binding rejects.
    _common_log("list: " + msg)


def _player_on():
    """True while audio/video plays OR a network track is still opening
    (bp.aload). WebDAV enrichment is skipped then: a flaky DAV server (CloudMe)
    answers 502 under concurrent requests, which breaks the stream open."""
    try:
        if xbmcgui.Window(10000).getProperty("bp.aload") == "1":
            return True
    except Exception:
        pass
    try:
        return bool(xbmc.getCondVisibility("Player.HasAudio | Player.HasVideo"))
    except Exception:
        return False

# Blacklist (data/blacklist.json) applies even when hidden-files is on.


def skin_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_blacklist():
    """Active blacklist patterns (data/ seed + user file, toggled-off removed)."""
    return blacklist.load_active()


def folder_cache_path():
    try:
        return state_file(FOLDER_CACHE)
    except Exception:
        return ""


def load_folder_cache():
    try:
        import foldersize
        data = foldersize.load_cache()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".mpg", ".mpeg", ".webm",
             ".flv", ".wmv", ".ts", ".m2ts", ".3gp", ".ogv"}
AUDIO_EXT = {".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".wav", ".opus",
             ".wma", ".aiff", ".aif"}
HEIC_EXT = {".heic", ".heif", ".hif"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
             ".avif", ".heic", ".heif", ".hif"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"}


def item_url(path):
    """Click/RunScript channel: path percent-encoded TWICE (Kodi trims commas
    and percent-decodes once in skin commands); main.py _decode unquotes twice."""
    return quote(quote(path, safe="", errors="surrogateescape"), safe="")


def core_url(path):
    """Item URL for the Kodi core: native raw path, else encoded fallback
    (raw undecodable names segfault the binding; encoded URLs break zip:// probes)."""
    try:
        path.encode("utf-8")
        return path
    except Exception:
        return quote(path, safe="", errors="surrogateescape")


def kind_of(name):
    """Row icon type for the bp.kind property (mirrors fileops.py extension sets)."""
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
            # HEIC without a decoder is shown as a generic file, not a photo.
        if ext in HEIC_EXT and not heic_capable():
            return "file"
        return "photo"
    if ext in ARCHIVE_EXT:
        return "archive"
    return "file"


def size_to_string(size):
    prefixes = (" ", "k", "M", "G", "T", "P", "E", "Z", "Y")
    i = 0
    s = float(size)
    while i < len(prefixes) and s >= 1000.0:
        s /= 1024.0
        i += 1
    if i == 0:
        return "%.2f B" % s
    if i >= len(prefixes):
        if s >= 1000.0:
            return ">999.99 %sB" % prefixes[-1]
        return "%.2f %sB" % (s, prefixes[-1])
    if s >= 100.0:
        return "%.1f %sB" % (s, prefixes[i])
    return "%.2f %sB" % (s, prefixes[i])


MAX_MATCHES = 200


def _is_picker():
    try:
        return xbmcgui.Window(10000).getProperty("bp.pick.active") == "1"
    except Exception:
        return False


def netsize_on():
    """Network sizes/dates toggle (Sources tab); distinct from folder sizes."""
    try:
        return bool(xbmc.getCondVisibility("Skin.HasSetting(netsize.on)"))
    except Exception:
        return False


def sizes_on():
    """True when folder sizes are enabled (settings opt-in, default OFF)."""
    try:
        return bool(xbmc.getCondVisibility("Skin.HasSetting(foldersize.on)"))
    except Exception:
        return False


def append_matches(items, root, needle, show_hidden=False, patterns=None, folder_cache=None, case_sensitive=False):
    """Recursive name search below root (files + folders, capped at MAX_MATCHES).
    Appends (item, path, is_folder, name, size, mtime)."""
    patterns = patterns or []
    folder_cache = folder_cache or {}
    sizes_flag = sizes_on()
    picker = _is_picker()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not blocked(d, patterns, case_sensitive)
                             and (show_hidden or not d.startswith(".")))
        for name in dirnames + sorted(f for f in filenames
                                      if not blocked(f, patterns, case_sensitive)
                                      and (show_hidden or not f.startswith("."))):
            try:
                if needle not in name.lower():
                    continue
                full = os.path.join(dirpath, name)
                is_dir = name in dirnames
                label = safe_label(name)
                item = xbmcgui.ListItem(label)
                item.setIsFolder(is_dir)
                if not is_dir:
                    item.setProperty("bp.kind", kind_of(label))
                size = 0
                mtime = 0
                try:
                    st = os.stat(full)
                    mtime = st.st_mtime
                    if is_dir:
                        if sizes_flag:
                            cached = folder_cache.get(full)
                            if cached and isinstance(cached.get("size"), int):
                                size = cached["size"]
                                if cached.get("partial") or cached.get("approx"):
                                    item.setLabel2("~ " + size_to_string(size))
                                else:
                                    item.setLabel2(size_to_string(size))
                            else:
                                size = 0
                    else:
                        size = st.st_size
                        item.setLabel2(size_to_string(size))
                    item.setDateTime(
                        datetime.datetime.fromtimestamp(st.st_mtime)
                        .strftime("%Y-%m-%dT%H:%M:%S"))
                except OSError:
                    pass
                try:
                    cm = [] if (picker and not is_dir) else context_menu(full)
                    item.addContextMenuItems(cm)
                    # Kodi hides "Add to favourites" when this property is set.
                    try:
                        item.setProperty("hide_add_remove_favourite", "true")
                    except Exception:
                        pass
                except Exception:
                    pass
                # onclick handlers read bp.url, never FileNameAndPath (see item_url).
                url = item_url(full)
                item.setProperty("bp.url", url)
                items.append((item, core_url(full), is_dir, natkey(label), size, mtime))
                if len(items) >= MAX_MATCHES:
                    log("match cap %d reached" % MAX_MATCHES)
                    return
            except Exception as e:
                log("skip match in %s: %r (%s)" % (redact(root), redact(name), e))
                continue


def context_menu(path):
    """Secondary-click trigger: one native entry opening our overlay (main.py ctx).
    Kodi's native DialogContextMenu closes on any click, so actions live in the overlay."""
    return [(
        xbmc.getLocalizedString(31339),
        "RunScript(special://skin/scripts/main.py,ctx,%s)" % item_url(path),
    )]


def context_menu_dotdot(path=None):
    """Secondary-click trigger for the ".." row (picker: add current folder as
    source; else reduced menu)."""
    if path is not None:
        return [(
            xbmc.getLocalizedString(31424),
            "RunScript(special://skin/scripts/main.py,srcask,%s)" % item_url(path),
        )]
    return [(
        xbmc.getLocalizedString(31339),
        "RunScript(special://skin/scripts/main.py,ctxmenu)",
    )]


def context_menu_picker(path):
    """Picker context menu on a folder row: add it as a directory source."""
    return [(
        xbmc.getLocalizedString(31424),
        "RunScript(special://skin/scripts/main.py,srcask,%s)" % item_url(path),
    )]


# Schemes Kodi opens natively (kept in sync with sources.NET_SCHEMES).
def _localized(i):
    try:
        return xbmc.getLocalizedString(i)
    except Exception:
        return ""


# Schemes whose Kodi VFS handler is a separate add-on that must be installed.
_VFS_ADDON = {"sftp": "vfs.sftp"}


def _missing_vfs_addon(path):
    """The add-on id a scheme needs but which is not installed, or ""."""
    low = (path or "").lower()
    for scheme, addon in _VFS_ADDON.items():
        if low.startswith(scheme + "://"):
            try:
                # AddonIsEnabled is false for a missing OR disabled add-on (a
                # merely-installed-but-disabled add-on still answers
                # System.HasAddon true).
                if not xbmc.getCondVisibility("System.AddonIsEnabled(%s)" % addon):
                    return addon
            except Exception:
                pass
    return ""


def _notify_net_error(path, err):
    """Top-right notification for a failed network listing, with a per-path
    cooldown so repeated listings do not spam."""
    try:
        win = xbmcgui.Window(10000)
        now = time.time()
        try:
            last_t = float(win.getProperty("bp.neterr.t") or 0)
        except ValueError:
            last_t = 0
        if win.getProperty("bp.neterr.path") == path and now - last_t < 30:
            return
        win.setProperty("bp.neterr.path", path)
        win.setProperty("bp.neterr.t", str(now))
        host = sources.netsrc_host(path) or path
        xbmcgui.Dialog().notification(
            safe_label(_localized(31467)),  # "Connection error"
            "%s (%s)" % (safe_label(host), safe_label(err)),
            xbmcgui.NOTIFICATION_ERROR, 10000)
    except Exception:
        pass


def _dav_entries_from_details(path, details):
    """Ordered DAV children [(vfs, disp, is_dir, size, mtime)] from a
    dav_details() answer, or None when the answer is missing/partial.

    Kodi's VFS lists a WebDAV child whose name holds a raw `;` only up to the
    `;` (it parses the rest as URL options), so such files showed truncated.
    The PROPFIND carries the true names; the child URL keeps the server href
    form verbatim (percent-encoded for non-ASCII like `%C3%84`, the raw `;`
    intact) -- a live check proved Kodi's PLAYER opens the raw form while `%3B`
    makes playback fail -- and only the DISPLAY name is decoded."""
    if not details:
        return None
    folder = unquote(sources.rstrip_slash(path))
    out = []
    self_seen = False
    for key, (size, mtime, is_dir) in details.items():
        if not key.startswith(("dav://", "davs://")):
            continue
        if unquote(sources.rstrip_slash(key)) == folder:
            self_seen = True
            continue
        raw = key.rstrip("/").rsplit("/", 1)[-1]
        if not raw:
            continue
        out.append((raw, unquote(raw), bool(is_dir), size, mtime))
    # A real DAV listing always echoes the collection itself; its absence means a
    # partial (transient 502) answer, so fall back to the VFS listing instead.
    return out if self_seen else None


def list_network(handle, path, picker_active=False):
    """List a network source over Kodi's VFS (ftp/ftps/smb/nfs/http/https/dav).
    Subfolders browse via the same bp.url channel; ".." stops at the host boundary."""
    win = xbmcgui.Window(10000)
    try:
        src = sources.rstrip_slash(path_dec(win.getProperty("bp.src") or ""))
    except Exception:
        src = ""
    dirs, files, err = [], [], ""
    is_dav = False
    dav_entries = None
    details = {}
    ftp_details = None
    ftp_ok = False   # OUR ftp/ftps listing (MLSD or LIST) succeeded
    from_cache = False
    cached_details = {}
    via = ""
    net_s = 0.0
    build_s = 0.0
    t_list = time.time()
    path = (path or "").strip()
    # Serve a cached folder BEFORE the reachability probe: while a file streams, a
    # source with a low connection limit (e.g. a FRITZ!Box FTP) can refuse the
    # probe, and a cached folder must still show -- a transient probe failure is
    # not "unreachable".
    if path and not path.endswith("://"):
        path = sources.rstrip_slash(path)
        is_dav = path.lower().startswith(("dav://", "davs://"))
        tail = path.rstrip("/").rsplit("/", 1)[-1] if is_dav else ""
        hit = listing_get(path, ttl=0)
        # A VFS-sourced DAV entry (no PROPFIND details) is invalid: it carries the
        # WebDAV self-reference and no sizes/dates -> re-list via PROPFIND.
        if hit is not None and is_dav and not hit[2]:
            hit = None
    else:
        hit = None
    if hit is not None:
        dirs, files, cached_details = hit
        from_cache = True
        via = "cache"
        if cached_details:
            # Rebuild the size/date map (child URL -> (size, mtime, is_dir)) so a
            # cache hit shows them too, not just a cold FTP listing.
            base = path.rstrip("/")
            details = {base + "/" + n: tuple(v)
                       for n, v in cached_details.items()}
            if is_dav:
                # A DAV listing has no dirs/files (it uses dav_entries); rebuild
                # it from the cache, else a cached DAV folder renders empty.
                dav_entries = [(n, unquote(n), bool(v[2]), v[0], v[1])
                               for n, v in cached_details.items()]
    elif not path or path.endswith("://"):
        # "no path" and "scheme only" ("ftp://") are half-filled entries: report
        # unreachable immediately instead of probing the VFS (long connect timeout).
        err = "unreachable"
    elif not sources.net_reachable(path):
        # Wrong network / dead host: Kodi's VFS connect can hold the listing (and
        # its spinner) for a long time, so a quick TCP probe to the host:port
        # fails fast instead of hanging on the VFS timeout.
        err = "unreachable"
    else:
        # WebDAV: ONE Depth-1 PROPFIND gives the TRUE child names (Kodi's VFS
        # truncates a name at a raw ";" -- its URL options separator) plus
        # sizes/dates. Use it as the listing source; the VFS listing is the
        # fallback when the PROPFIND is unavailable or partial.
        if is_dav and not _player_on():
            try:
                details = sources.dav_details(path, timeout=2, attempts=1)
            except Exception as e:
                details = {}
                log("dav details crashed: %s" % e)
            dav_entries = _dav_entries_from_details(path, details)
        if dav_entries is not None:
            err = ""
        else:
            # A DAV server (CloudMe) can answer a PROPFIND with a transient 502;
            # retry instead of showing an error -- or, worse, a fake EMPTY folder.
            # Time-bounded: one stalled request can already burn Kodi's timeout, so
            # a budget (not an attempt count) caps how long we keep trying.
            deadline = time.time() + 6.0
            attempt = 0
            # Kodi's vfs.sftp (and some others) build child paths by appending the
            # name to the folder STRING: list the slash form (see sources.vfs_dir).
            ls_path = sources.vfs_dir(path)
            ftp_src = sources.is_ftp(path)
            while True:
                attempt += 1
                try:
                    # FTP: our own MLSD listing -- Kodi's FTP listing truncates a
                    # child name at "?"/";" (its URL option/query separators) and
                    # cannot stat them; MLSD gives the true names + sizes/dates.
                    res = None
                    _t_net = time.time()
                    if ftp_src:
                        r3 = sources.ftp_listdir(path)
                        if r3[0] is not None:
                            res, ftp_details = (r3[0], r3[1]), r3[2]
                            ftp_ok = True
                    if res is None:
                        res = xbmcvfs.listdir(ls_path)
                    net_s += time.time() - _t_net
                except Exception as e:
                    res = None
                    err = str(e) or "error"
                if (isinstance(res, tuple) and len(res) == 2
                        and res[0] is not False and res[1] is not False):
                    dirs = list(res[0] or [])
                    files = list(res[1] or [])
                    # A real DAV listing echoes the collection itself (PROPFIND
                    # self-reference). Missing -> partial answer (transient 502):
                    # treat it as a failure and retry, never as an empty folder.
                    if is_dav and tail and tail not in dirs and tail not in files:
                        err = "unreachable"
                    else:
                        if is_dav and tail in dirs:
                            # Drop ONE self-reference: a real child may share the
                            # folder's name (Movies3/Movies3) and must survive.
                            dirs.remove(tail)
                        err = ""
                        if attempt > 1:
                            log("network: listdir ok on attempt %d (%s)"
                                % (attempt, redact(path)))
                        break
                else:
                    err = "unreachable"
                if time.time() >= deadline:
                    break
                time.sleep(0.4)
    if not err and not dirs and not files and dav_entries is None:
        # An empty result is a real folder only when it exists -- but a DAV
        # listing already carried the collection itself (PROPFIND self-ref, just
        # stripped), so an empty DAV result IS an existing empty folder; its
        # exists() probe is unreliable (CloudMe empty folders read unreachable).
        # Skip the exists() probe where Kodi's Stat is unreliable: a DAV listing
        # already carried the collection, vfs.sftp returns False even for an
        # EXISTING empty folder (verified), and Kodi's FTP Stat does the same. An
        # empty ftp/ftps answer is a real empty folder ONLY when OUR client
        # listed it (MLSD/LIST, `ftp_ok`): if our client failed (e.g. an FTPS
        # certificate error) and Kodi's VFS answered an empty tuple, the folder is
        # not proven to exist -- the probe then reports unreachable instead of a
        # fake empty folder. For ftp/ftps the earlier TCP probe already ruled out
        # a dead host.
        ftp_proven = sources.is_ftp(path) and ftp_ok
        if not is_dav and not ftp_proven and not path.lower().startswith("sftp://"):
            try:
                if not xbmcvfs.exists(path):
                    err = "unreachable"
            except Exception as e:
                err = str(e) or "error"
    # vfs.sftp returns an EMPTY listing for an unreachable host (wrong network)
    # with NO error, so a dead connection looks like an empty folder; a quick TCP
    # probe to the host:port tells them apart.
    if (not err and not dirs and not files and dav_entries is None
            and path.lower().startswith("sftp://")):
        if not sources.host_reachable(path):
            err = "unreachable"

    if not from_cache and not err:
        via = "ftp" if ftp_ok else "vfs"

    # Share the listing with the player so starting a file does not re-list this
    # (slow) network folder -- see common.listing_get. A cache hit is already
    # stored, so only a fresh listing is written back.
    if not err and not from_cache:
        try:
            if dav_entries is not None:
                _det = {vfs: [s, m, d]
                        for (vfs, _disp, d, s, m) in dav_entries}
            else:
                _det = {n: list(v) for n, v in (ftp_details or {}).items()}
            listing_put(path, dirs, files, _det)
        except Exception:
            pass

    missing = _missing_vfs_addon(path)
    if missing:
        # The scheme's VFS add-on is missing or disabled: Kodi then reports
        # nothing (an EMPTY listing on some platforms), so always name it.
        err = "%s (%s)" % (_localized(31545), missing)
    if err and not path.endswith("://"):
        _notify_net_error(path, err)
    # FTP: the MLSD answer already carries sizes/dates for the whole folder
    # (keyed by the child URL, like the WebDAV PROPFIND details).
    if ftp_details and not details:
        base = path.rstrip("/")
        details = {base + "/" + n: v for n, v in ftp_details.items()}

    query = sys.argv[0] + (sys.argv[2] if len(sys.argv) > 2 else "")
    show_hidden = search.hidden_from_url(query)
    sort = search.sort_from_url(query)
    folders_first_flag = search.foldersfirst_from_url(query)
    case_sensitive = search.casesensitive_from_url(query)
    patterns = load_blacklist()
    # Size/date come from the background foldersize cache (~200 ms per VFS
    # stat); both stay empty until the data is read. Own setting, independent
    # of folder sizes.
    sizes_flag = sizes_on()
    netsize_flag = netsize_on()
    folder_cache = load_folder_cache() if (sizes_flag or netsize_flag) else {}
    # WebDAV: the PROPFIND above already carries sizes + dates for the whole
    # folder (folders too); persist them so they survive a later failed
    # PROPFIND (server 503s when busy).
    if details and netsize_flag:
        try:
            merged = dict(folder_cache)
            for key, (dsize, dmtime, ddir) in details.items():
                if not key.startswith(("dav://", "davs://")):
                    continue        # name keys only serve the lookup
                entry = {"size": 0 if ddir else dsize, "mtime": dmtime}
                if ddir:
                    entry["dir"] = True
                    entry["tried"] = True
                merged[cache_key(key.rstrip("/"))] = entry
            write_json(folder_cache_path(), merged)
            folder_cache = merged
        except Exception:
            pass

    t_build = time.time()
    items = []
    skipped_hidden = skipped_blocked = 0
    if err:
        item = xbmcgui.ListItem("%s: %s" % (_localized(31467), err))
        item.setIsFolder(False)
        item.setProperty("bp.error", "1")
        items.append((item, "dummy://network/error", False, ""))
    else:
        parent = sources.net_parent(path)
        if path != src and parent:
            item = xbmcgui.ListItem("..")
            item.setIsFolder(True)
            item.setProperty("bp.dotdot", "1")
            try:
                item.addContextMenuItems(context_menu_dotdot(None))
                item.setProperty("hide_add_remove_favourite", "true")
            except Exception:
                pass
            items.append((item, core_url(parent), True, ""))
        if dav_entries is not None:
            entries = [(vfs, is_dir, size, mtime)
                       for (vfs, _disp, is_dir, size, mtime) in dav_entries]
            disp_map = {vfs: disp
                        for (vfs, disp, _d, _s, _m) in dav_entries}
        else:
            entries = ([(n, True, None, None) for n in dirs]
                       + [(n, False, None, None) for n in files])
            disp_map = {}
        kept = []
        for name, is_dir, size0, mtime0 in entries:
            if name.startswith(".") and not show_hidden:
                skipped_hidden += 1
                continue
            if blocked(name, patterns, case_sensitive):
                skipped_blocked += 1
                continue
            kept.append((name, is_dir, size0, mtime0))
        # No VFS size/date (extra round trip per entry), so those sorts fall back to name.
        if sort in ("size", "date"):
            log("network sort '%s' falls back to name (no VFS size/date)" % sort)
        folders_first = bool(folders_first_flag)
        # Use the decoded display form for sorting; child URLs stay href-encoded.
        kept.sort(key=lambda e: ((not e[1],) if folders_first else ())
                  + (natkey(safe_label(unquote(e[0]) if is_dav else e[0])),))
        for name, is_dir, size0, mtime0 in kept:
            try:
                # The picker shows files too (context); a file click is inert there
                # (the list onclick only opens outside picking).
                # WebDAV: the VFS segment is URL-form (a raw `;` in it would be
                # parsed as options); only the display name is decoded.
                disp = disp_map.get(name)
                if disp is None:
                    disp = unquote(name) if is_dav else name
                item = xbmcgui.ListItem(safe_label(disp))
                item.setIsFolder(is_dir)
                # Explicit local artwork: without art Kodi probes "<itemurl>.tbn"
                # per row -- a real network request on a VFS URL.
                kind = "folder" if is_dir else kind_of(disp)
                if kind not in ("folder", "video", "audio", "photo"):
                    kind = "file"
                # Filetype artwork uses the original Bootstrap icon names.
                icon = "special://skin/media/filetypes/%s.png" % {
                    "folder": "folder", "video": "file-play",
                    "audio": "file-music", "photo": "file-image"}.get(kind, "file-text")
                try:
                    item.setArt({"thumb": icon, "icon": icon})
                except Exception:
                    pass
                if not is_dir and kind in ("video", "audio"):
                    # Stop the core tag loader: without a media info tag it
                    # probes every file over the network (display-neutral).
                    try:
                        # safe_label: a raw surrogate (undecodable byte in the name)
                        # segfaults the setInfo binding.
                        item.setInfo(
                            "video" if kind == "video" else "music",
                            {"title": safe_label(disp)})
                    except Exception:
                        pass
                if is_dir:
                    # Kodi does NOT honour the folder flag for plugin items with a
                    # VFS URL, so bp.dir carries it explicitly for the skin.
                    item.setProperty("bp.dir", "1")
                else:
                    item.setProperty("bp.kind", kind_of(disp))
                child = path + "/" + name
                try:
                    # Sizes/dates: the PROPFIND answer (dav_entries) first, else a
                    # fresh lookup, else the background cache.
                    size = mtime = None
                    if netsize_flag and size0 is not None:
                        size, mtime = size0, mtime0
                    if size is None and netsize_flag and details:
                        # the server lists a collection with a trailing slash
                        d = (details.get(child) or details.get(child + "/")
                             or details.get(disp))
                        if d:
                            size, mtime = d[0], d[1]
                    if size is None:
                        cached = folder_cache.get(cache_key(child))
                        if cached:
                            size = cached.get("size")
                            mtime = cached.get("mtime")
                    if mtime:
                        item.setDateTime(datetime.datetime.fromtimestamp(
                            mtime).strftime("%Y-%m-%dT%H:%M:%S"))
                    if not is_dir and isinstance(size, int):
                        item.setLabel2(size_to_string(size))
                except Exception:
                    pass
                item.setProperty("bp.url", item_url(child))
                # Same context menu as local; overlay hides write rows for network paths.
                if not picker_active:
                    try:
                        item.addContextMenuItems(context_menu(child))
                        item.setProperty("hide_add_remove_favourite", "true")
                    except Exception:
                        pass
                items.append((item, core_url(child), is_dir, natkey(safe_label(disp))))
            except Exception as e:
                log("skip network entry %r: %s" % (redact(name), e))
                continue
    # Padding rows: 2 top (top bar overlay; also for the single error row) plus
    # the overlaid bottom bars (audio footer 200px = 3 rows, picker 100px = 1 row).
    for i in range(2):
        pad = xbmcgui.ListItem("")
        pad.setIsFolder(False)
        pad.setProperty("bp.padding", "1")
        items.insert(0, (pad, "dummy://padding/top%d" % i, False, ""))
    try:
        # Audio footer padding: also while a network track still loads
        # (bp.aload) -- the footer shows from the start there.
        audio_bar = ((xbmc.getCondVisibility("Player.HasAudio")
                      and not xbmc.getCondVisibility("Player.HasVideo"))
                     or win.getProperty("bp.aload") == "1")
        if audio_bar and not picker_active:
            for i in range(3):
                pad = xbmcgui.ListItem("")
                pad.setIsFolder(False)
                pad.setProperty("bp.padding", "1")
                items.append((pad, "dummy://padding/audio%d" % i, False, ""))
        if picker_active:
            pad = xbmcgui.ListItem("")
            pad.setIsFolder(False)
            pad.setProperty("bp.padding", "1")
            items.append((pad, "dummy://padding/picker0", False, ""))
    except Exception:
        pass

    _publish_playing_pos(items)
    try:
        win.setProperty("bp.list.path", cache_key(path))
    except Exception:
        pass
    total = len(items)
    for item, url, is_folder, _ in items:
        xbmcplugin.addDirectoryItem(handle, url, item, is_folder, total)
    xbmcplugin.endOfDirectory(handle, True)
    build_s = time.time() - t_build
    log("network %d items from %s (list %.1fs, net %.1fs, build %.1fs, via=%s)%s (hidden=%s, sort=%s, ff=%s, cs=%s, blacklist=%d, skipped_hidden=%d, skipped_blocked=%d)"
        % (total, redact(path), time.time() - t_list, net_s, build_s, via or "-",
           (" err=%s" % err) if err else "", show_hidden, sort,
           folders_first_flag, case_sensitive, len(patterns),
           skipped_hidden, skipped_blocked))

    # Stale-while-revalidate: the cached list is on screen; re-list in the
    # background (only when the cache is old) and reload if the folder changed.
    if (from_cache and not err and not picker_active
            and (listing_get(path) is None or not cached_details)):
        _refresh_cached_listing(path, dirs, files, bool(cached_details), win)


def _fresh_net_names(path):
    """(dirs, files, details) from a fresh network listing: our MLSD/LIST for ftp
    and a PROPFIND for dav (both carry sizes/dates), else the VFS (none). DAV must
    go through the PROPFIND, not the VFS, which adds the collection self-reference
    and drops the sizes/dates."""
    if path.lower().startswith(("dav://", "davs://")):
        try:
            entries = _dav_entries_from_details(
                path, sources.dav_details(path, timeout=6, attempts=1))
        except Exception:
            entries = None
        if entries is None:
            return [], [], {}
        dirs = [vfs for (vfs, _d, isd, _s, _m) in entries if isd]
        files = [vfs for (vfs, _d, isd, _s, _m) in entries if not isd]
        details = {vfs: [s, m, isd] for (vfs, _d, isd, s, m) in entries}
        return dirs, files, details
    try:
        if sources.is_ftp(path):
            r3 = sources.ftp_listdir(path)
            if r3[0] is not None:
                return list(r3[0]), list(r3[1]), dict(r3[2] or {})
    except Exception:
        pass
    try:
        res = xbmcvfs.listdir(sources.vfs_dir(path))
        if isinstance(res, tuple) and len(res) == 2 and res[0] is not False:
            return list(res[0] or []), list(res[1] or []), {}
    except Exception:
        pass
    return [], [], {}


def _refresh_cached_listing(path, dirs, files, had_details, win):
    """Re-list a cached folder in the background and update its cache (names AND
    sizes/dates); reload the container only if something changed (names, or the
    sizes/dates that were missing before), and only while the same folder is
    still shown."""
    try:
        ndirs, nfiles, ndet = _fresh_net_names(path)
    except Exception:
        return
    if not ndirs and not nfiles:
        return
    changed = (sorted(ndirs) != sorted(dirs)
               or sorted(nfiles) != sorted(files)
               or (not had_details and bool(ndet)))
    try:
        listing_put(path, ndirs, nfiles, ndet)
    except Exception:
        pass
    if not changed:
        return
    try:
        if win.getProperty("bp.list.path") != cache_key(path):
            return   # user navigated away: never reload a different folder
        win.setProperty("bp.refresh", str(time.time()))
        xbmc.executebuiltin("Container.Refresh")
        log("network: refreshed cached listing %s" % redact(path))
    except Exception:
        pass


def _publish_playing_pos(items):
    """Store the absolute list position of the playing (or last-played) file so
    the footer Up can focus it (SetFocus(33,pos,absolute)); -1 clears it. The
    position counts the invisible padding rows."""
    win = xbmcgui.Window(10000)
    # Item basenames (one per line; padding rows empty) so the daemon can focus
    # the playing row on a track change without reloading the list.
    try:
        win.setProperty("bp.list.keys", "\n".join(
            unquote(os.path.basename(str(it[1]).rstrip("/"))) for it in items))
    except Exception:
        pass
    target = ""
    try:
        if xbmc.getCondVisibility("Player.HasAudio | Player.HasVideo"):
            target = xbmc.getInfoLabel("Player.FilenameAndPath") or ""
    except Exception:
        target = ""
    if not target:
        target = win.getProperty("bp.lastplayed") or ""
    pos = -1
    if target:
        # Match the raw core URL (it[1]), not bp.url: the latter is percent-encoded
        # twice for the RunScript channel and would never equal the playing path.
        base = unquote(os.path.basename(target.rstrip("/")))
        for i, it in enumerate(items):
            if unquote(os.path.basename(str(it[1]).rstrip("/"))) == base:
                pos = i
                break
    if pos >= 0:
        win.setProperty("bp.playing.pos", str(pos))
    else:
        win.clearProperty("bp.playing.pos")
    log("playing pos %d target=%s" % (pos, redact(target)))


def main():
    handle = int(sys.argv[1]) if len(sys.argv) > 1 else -1
    # DEV: source self-test hook -- run scripts/selftest.py for a target:
    #   plugin://browsybare/list?selftest=<target>   (see dev/source-selftest.sh)
    import re as _re
    import urllib.parse as _up
    _query = sys.argv[0] + (sys.argv[2] if len(sys.argv) > 2 else "")
    _m = _re.search(r"[?&]selftest=([^&]+)", _query)
    if _m:
        try:
            import selftest as _st
            _st.run(_up.unquote(_m.group(1)))
        except Exception as _e:
            log("selftest hook crashed: %s" % _e)
        return
    items = []
    dotdot = None
    raw = path_dec(xbmcgui.Window(10000).getProperty("bp.path") or "")
    picker_active = _is_picker()
    # Match network paths on the RAW value: stripping the trailing "/" turns a
    # half-filled "ftp://" into "ftp:", which is_network_path() no longer recognises.
    if is_network_path(raw) or (not raw and
                                xbmcgui.Window(10000).getProperty("bp.net") == "1"):
        list_network(handle, raw, picker_active)
        return
    path = raw.rstrip("/")
    needle = show_hidden = sort = None
    folders_first_flag = False
    case_sensitive = False
    patterns = []
    skipped_hidden = 0
    skipped_blocked = 0
    if path and os.path.isdir(path):
        # The entered source root (bp.src) is root-like: no ".." row there.
        try:
            src = path_dec(xbmcgui.Window(10000).getProperty("bp.src") or "").rstrip("/")
        except Exception:
            src = ""
        if path != drive_root_of(path) and path != src:
            parent = os.path.dirname(path)
            if parent and parent not in ("/", "/Volumes") and os.path.isdir(parent):
                item = xbmcgui.ListItem("..")
                item.setIsFolder(True)
                item.setProperty("bp.dotdot", "1")
                try:
                    item.addContextMenuItems(
                        context_menu_dotdot(path if picker_active else None))
                    item.setProperty("hide_add_remove_favourite", "true")
                except Exception:
                    pass
                dotdot = (item, core_url(parent), True, "..", 0, 0)
        url = sys.argv[0] + (sys.argv[2] if len(sys.argv) > 2 else "")
        needle = search.needle_from_url(url)
        show_hidden = search.hidden_from_url(url)
        sort = search.sort_from_url(url)
        folders_first_flag = search.foldersfirst_from_url(url)
        case_sensitive = search.casesensitive_from_url(url)
        patterns = load_blacklist()
        sizes_flag = sizes_on()
        folder_cache = load_folder_cache() if sizes_flag else {}
        if needle:
            append_matches(items, path, needle, show_hidden, patterns, folder_cache, case_sensitive)
        else:
            try:
                # Sort by the natural folded display name (natkey), not raw codepoints.
                entries = sorted(os.scandir(path), key=lambda e: natkey(safe_label(e.name)))
            except OSError as e:
                log("scandir failed for %s: %s" % (redact(path), e))
                entries = []
            skipped_hidden = 0
            skipped_blocked = 0
            for entry in entries:
                try:
                    raw = entry.name
                    if raw.startswith(".") and not show_hidden:
                        skipped_hidden += 1
                        continue
                    if blocked(raw, patterns, case_sensitive):
                        skipped_blocked += 1
                        continue
                    name = safe_label(raw)
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    # The picker shows files too (context); a file click is inert
                    # there (the list onclick only opens outside picking).
                    item = xbmcgui.ListItem(name)
                    item.setIsFolder(is_dir)
                    if not is_dir:
                        item.setProperty("bp.kind", kind_of(name))
                    size = 0
                    mtime = 0
                    try:
                        st = entry.stat()
                        mtime = st.st_mtime
                        if is_dir:
                            if sizes_flag:
                                cached = folder_cache.get(entry.path)
                                if cached and isinstance(cached.get("size"), int):
                                    size = cached["size"]
                                    if cached.get("partial") or cached.get("approx"):
                                        item.setLabel2("~ " + size_to_string(size))
                                    else:
                                        item.setLabel2(size_to_string(size))
                                else:
                                    size = 0
                        else:
                            size = st.st_size
                            item.setLabel2(size_to_string(size))
                        item.setDateTime(
                            datetime.datetime.fromtimestamp(st.st_mtime)
                            .strftime("%Y-%m-%dT%H:%M:%S"))
                    except OSError:
                        pass
                    try:
                        if picker_active:
                            # Only folders are a pick target; a file gets no menu.
                            cm = context_menu_picker(entry.path) if is_dir else []
                        else:
                            cm = context_menu(entry.path)
                        item.addContextMenuItems(cm)
                        try:
                            item.setProperty("hide_add_remove_favourite", "true")
                        except Exception:
                            pass
                    except Exception:
                        pass
                    # onclick handlers read bp.url, never FileNameAndPath (see item_url).
                    url = item_url(entry.path)
                    item.setProperty("bp.url", url)
                    items.append((item, core_url(entry.path), is_dir, natkey(name), size, mtime))
                except Exception as e:
                    # One bad entry must never kill the listing -- Kodi segfaults
                    # on some inside its bindings instead of raising.
                    log("skip entry in %s: %r (%s)" % (redact(path), redact(entry.name), e))
                    continue
    # Sorting: .. on top; folders-first groups in every mode, then the mode's
    # key; ff travels via URL like h (reload trigger).
    folders_first = bool(folders_first_flag)
    def sort_key(x):
        prefix = (not x[2],) if folders_first else ()
        if sort == "size":
            return prefix + (-x[4], x[3])
        if sort == "date":
            return prefix + (-x[5], x[3])
        return prefix + (x[3],)
    items.sort(key=sort_key)
    if dotdot:
        items.insert(0, dotdot)
    # Top padding: 2 invisible rows (2*68=136 > top bar 128) push the first
    # real row below the bar; exactly two keeps zebra parity.
    if path and os.path.isdir(path):
        for i in range(2):
            pad = xbmcgui.ListItem("")
            pad.setIsFolder(False)
            pad.setProperty("bp.padding", "1")
            items.insert(0, (pad, "dummy://padding/top%d" % i, False, "", 0, 0))
    # Bottom-bar padding: audio footer (200px, top 880) -> 3 rows; picker bar
    # (100px at 980) -> 1 row; added independently (picker+audio = 4).
    try:
        has_audio = xbmc.getCondVisibility("Player.HasAudio")
        has_video = xbmc.getCondVisibility("Player.HasVideo")
        if has_audio and not has_video and not needle and not picker_active:
            for i in range(3):
                pad = xbmcgui.ListItem("")
                pad.setIsFolder(False)
                pad.setProperty("bp.padding", "1")
                items.append((pad, "dummy://padding/audio%d" % i, False, "", 0, 0))
        if picker_active and not needle:
            for i in range(1):
                pad = xbmcgui.ListItem("")
                pad.setIsFolder(False)
                pad.setProperty("bp.padding", "1")
                items.append((pad, "dummy://padding/picker%d" % i, False, "", 0, 0))
    except Exception:
        pass
    _publish_playing_pos(items)
    total = len(items)
    for item, url, is_folder, _, _, _ in items:
        xbmcplugin.addDirectoryItem(handle, url, item, is_folder, total)
    xbmcplugin.endOfDirectory(handle, True)
    log("%d items from %s (needle=%r, hidden=%s, sort=%s, ff=%s, cs=%s, blacklist=%d, skipped_hidden=%d, skipped_blocked=%d)"
        % (total, redact(path), needle, show_hidden, sort, folders_first_flag, case_sensitive, len(patterns),
           skipped_hidden, skipped_blocked))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("ERROR: %s" % e)
        try:
            xbmcplugin.endOfDirectory(int(sys.argv[1]), False)
        except Exception:
            pass
    finally:
        # Mark the list-loading veil (bp.listload) as "items built"; the
        # home-daemon drops it once Container(33).IsUpdating is false.
        try:
            win = xbmcgui.Window(10000)
            if win.getProperty("bp.listload") == "1":
                win.setProperty("bp.listload.ready", "1")
        except Exception:
            pass
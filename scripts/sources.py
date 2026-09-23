#!/usr/bin/env python3
"""Sources/drives data layer (no skin UI; main.py orchestrates the state).
drive-sources.json holds DRIVE entries only; other sources merge live."""
import hashlib
import os
import time
import sys
from urllib.parse import unquote

import xbmc
import xbmcvfs

from common import (read_json as _read_json, state_file as _state_file,
                    safe_label as safe_label, log as _log)
_safe_label = safe_label


def is_system_volume(path):
    """True when path lives on the boot volume (same device as /).

    macOS fallback (firmlinks can split st_dev): /System + /Applications present."""
    try:
        if os.stat(path).st_dev == os.stat("/").st_dev:
            return True
    except OSError:
        return False
    if sys.platform == "darwin":
        try:
            if os.path.isdir(os.path.join(path, "System")) and \
               os.path.isdir(os.path.join(path, "Applications")):
                return True
        except Exception:
            pass
    return False


# Pseudo filesystems that are never user drives.
PSEUDO_FS = {"tmpfs", "devtmpfs", "sysfs", "proc", "cgroup", "cgroup2",
             "devpts", "mqueue", "shm", "overlay", "tracefs", "debugfs",
             "securityfs", "fusectl", "configfs"}


# Apple system volume names, fallback when the mount table cannot be read.
_DARWIN_SYSTEM_NAMES = frozenset(
    {"Recovery", "Preboot", "VM", "Update", "xartStorage", "iSCPreboot", "Hardware"}
)

_NOBROWSE_CACHE = {"at": 0.0, "mounts": set()}
_NOBROWSE_TTL = 60.0


def _darwin_nobrowse_mounts():
    """Mount points macOS hides (nobrowse): invisible in Finder but still
    returned by os.listdir, so the skin must skip them too. Cached briefly."""
    try:
        now = time.time()
    except Exception:
        now = 0.0
    try:
        if now - float(_NOBROWSE_CACHE.get("at", 0.0)) < _NOBROWSE_TTL:
            return set(_NOBROWSE_CACHE.get("mounts", set()))
    except Exception:
        pass
    mounts = set()
    try:
        import subprocess
        proc = subprocess.run(["mount"], capture_output=True, text=True, timeout=5)
        for line in (proc.stdout or "").splitlines():
            if " on " not in line or "nobrowse" not in line:
                continue
            _, rest = line.split(" on ", 1)
            if " (" not in rest:
                continue
            mnt, opts = rest.rsplit(" (", 1)
            flags = [o.strip() for o in opts.rstrip(")").split(",")]
            if "nobrowse" in flags and mnt:
                mounts.add(mnt.rstrip("/"))
    except Exception:
        mounts = set()
    try:
        _NOBROWSE_CACHE["at"] = now
        _NOBROWSE_CACHE["mounts"] = set(mounts)
    except Exception:
        pass
    return mounts


def _is_android():
    """True on Android (Kodi reports sys.platform as linux there).

    Detected via ANDROID_ROOT/ANDROID_DATA or /system/build.prop, not the kernel."""
    try:
        if os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_DATA"):
            return True
    except Exception:
        pass
    try:
        return os.path.isfile("/system/build.prop")
    except Exception:
        return False


def _android_drives():
    """Android shared storage as drives: /storage/emulated/0 internal, other
    /storage/<id> removable; bare /storage/emulated skipped."""
    out = []
    try:
        internal = "/storage/emulated/0"
        if os.path.isdir(internal):
            try:
                label = xbmc.getLocalizedString(31447)
            except Exception:
                label = ""
            out.append({"label": safe_label(label or "Internal storage"),
                        "path": internal + "/", "type": "local", "system": False})
        try:
            names = sorted(os.listdir("/storage"))
        except OSError:
            names = []
        for name in names:
            if name == "emulated":
                continue
            path = "/storage/%s" % name
            try:
                is_dir = os.path.isdir(path)
            except Exception:
                continue
            if is_dir:
                out.append({"label": safe_label(name), "path": path + "/",
                            "type": "local", "system": False})
    except Exception:
        pass
    return out


def _unescape_mnt(s):
    """Decode /proc/mounts octal escapes (space etc. in volume labels)."""
    return (s.replace("\\040", " ").replace("\\012", "\n")
             .replace("\\011", "\t").replace("\\134", "\\"))


def _linux_mounts(bases):
    """Mountpoints below the media bases with real filesystems, any depth.
    Ubuntu nests at /media/<user>/<label>. Returns [(label, path)]."""
    out = []
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = _unescape_mnt(parts[1])
        if parts[2] in PSEUDO_FS:
            continue
        for base in bases:
            norm = base.rstrip("/")
            if mnt != base and mnt.startswith(norm + "/") and os.path.isdir(mnt):
                out.append((os.path.basename(mnt.rstrip("/")) or mnt, mnt))
                break
    return out


def _windows_drives():
    """Windows logical drives (fixed + removable + mapped network) via the
    GetLogicalDrives bitmask; CD-ROM/RAM skipped, each drive probed in isolation."""
    out = []
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
    except Exception:
        return out
    try:
        system_drive = (os.environ.get("SystemDrive", "C:").rstrip("\\") + "\\").upper()
    except Exception:
        system_drive = "C:\\"
    for i in range(26):
        if not (mask & (1 << i)):
            continue
        root = "%s:\\" % chr(ord("A") + i)
        try:
            dtype = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
            if dtype not in (2, 3, 4):  # removable, fixed, remote
                continue
            label = ""
            try:
                buf = ctypes.create_unicode_buffer(256)
                if kernel32.GetVolumeInformationW(
                        ctypes.c_wchar_p(root), buf, 256,
                        None, None, None, None, 0):
                    label = buf.value or ""
            except Exception:
                label = ""
            if not os.path.isdir(root):
                continue
            out.append({"label": safe_label(label or root), "path": root,
                        "type": "local",
                        "system": root.upper() == system_drive})
        except Exception:
            continue
    return out


def local_drives():
    """Locally mounted volumes (macOS /Volumes, Linux media bases, Android,
    Windows). macOS skips nobrowse + known Apple system names."""
    drives = []
    if sys.platform == "darwin":
        hidden_mounts = _darwin_nobrowse_mounts()
        try:
            names = sorted(os.listdir("/Volumes"))
        except OSError:
            # iOS/tvOS have no /Volumes: never fail the whole drive step.
            names = []
        for name in names:
            if name in _DARWIN_SYSTEM_NAMES:
                continue
            path = "/Volumes/%s" % name
            if path.rstrip("/") in hidden_mounts:
                continue
            if os.path.isdir(path):
                drives.append({"label": safe_label(name), "path": path + "/", "type": "local",
                               "system": is_system_volume(path)})
    elif sys.platform.startswith("linux"):
        if _is_android():
            # Android reports linux: use the fixed layout, not the mount table.
            drives.extend(_android_drives())
            return drives
        # Mount table only (no scandir fallback: it reports containers and
        # stale mountpoints as drives). Only real mounts surface.
        found = _linux_mounts(("/media", "/run/media", "/var/media", "/mnt", "/storage"))
        seen = set()
        for label, path in sorted(found):
            if path not in seen:
                seen.add(path)
                drives.append({"label": safe_label(label), "path": path + "/", "type": "usb",
                               "system": is_system_volume(path)})
    elif sys.platform == "win32":
        drives.extend(_windows_drives())
    return drives


def home_drive():
    """The user home folder as a source (type home, topmost when visible).

    Visible by default (opt-out hide.home); ~ always exists, unlike drives."""
    try:
        home = os.path.expanduser("~")
    except Exception:
        return None
    if not home or not os.path.isdir(home):
        return None
    try:
        label = xbmc.getLocalizedString(31363)
    except Exception:
        label = ""
    if not label:
        label = "Home"
    return {"label": label,
            "path": home.rstrip("/") + "/", "type": "home"}


def network_sources():
    """User-defined network sources from network-sources.json (label required,
    path may be empty). `key` is the visibility identity (netsrc_key)."""
    out = []
    data = _read_json(_state_file("network-sources.json"), [])
    if not isinstance(data, list):
        return out
    for idx, s in enumerate(data):
        if isinstance(s, dict) and s.get("label"):
            entry = {"label": str(s["label"]), "path": str(s.get("path") or "")}
            # Drop non-network non-empty paths (they would list empty as local);
            # path-less entries stay (display/toggle only).
            if entry["path"] and not is_network_path(entry["path"]):
                continue
            out.append({"label": entry["label"], "path": entry["path"],
                        "type": "network", "slot": idx + 1,
                        "key": netsrc_key(entry, idx + 1)})
    return out


# Schemes Kodi opens natively (VFS); netsrc_add rejects everything else.
NET_SCHEMES = ("ftp://", "ftps://", "smb://", "nfs://",
               "dav://", "davs://")

# Read-only Kodi VFS schemes: CCurlFile (ftp/ftps) has no Delete/Rename and
# CFTPDirectory no Create/Remove; the context menu greys those rows.
RO_SCHEMES = ("ftp://", "ftps://")


def is_network_path(path):
    """True when the path is a Kodi VFS network URL (single source of truth)."""
    try:
        return bool(path) and path.lower().startswith(NET_SCHEMES)
    except Exception:
        return False


def is_readonly(path):
    """True for network schemes without write support (ftp/ftps)."""
    try:
        return bool(path) and path.lower().startswith(RO_SCHEMES)
    except Exception:
        return False


def url_display(path):
    """Human-readable path for DISPLAY only: network URLs stay percent-encoded
    for Kodi's VFS, only displayed text is decoded. Local paths pass through."""
    try:
        return unquote(path) if is_network_path(path) else path
    except Exception:
        return path


def dav_details(url, timeout=8, attempts=2):
    """{key: (size, mtime, is_dir)} for a WebDAV collection via ONE PROPFIND
    (Depth 1). Keys are the full child URL and the decoded name; {} on failure."""
    try:
        import base64
        import ssl
        import urllib.request
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        from urllib.parse import unquote, urlsplit
    except Exception:
        return {}
    try:
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.path:
            # a davs://dav:// login URL -> its http(s) equivalent
            scheme = "https" if url.lower().startswith("davs://") else "http"
            rest = url.split("://", 1)[1] if "://" in url else url
            u = urlsplit("%s://%s" % (scheme, rest))
        host = u.hostname or ""
        if not host:
            return {}
        netloc = host + (":%d" % u.port if u.port else "")
        path = u.path or "/"
        if not path.endswith("/"):
            path += "/"
        request_url = "%s://%s%s" % (u.scheme, netloc, path)
        auth = ""
        if u.username is not None:
            userinfo = "%s:%s" % (unquote(u.username), unquote(u.password or ""))
            auth = "Basic " + base64.b64encode(
                userinfo.encode("utf-8")).decode("ascii")
        req = urllib.request.Request(request_url, method="PROPFIND")
        req.add_header("Depth", "1")
        req.add_header("Content-Type", "text/xml; charset=utf-8")
        if auth:
            req.add_header("Authorization", auth)
        body = None
        last = ""
        for _attempt in range(max(1, attempts)):
            try:
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=ssl.create_default_context()) as r:
                    body = r.read()
            except ssl.SSLError as e:
                # NO silent TLS downgrade: the request carries credentials, so a
                # verification bypass would defeat MITM protection. Treat as failure.
                last = "%s: %s" % (type(e).__name__, e)
            except Exception as e:
                # a 503 from an overloaded server is transient: retry
                last = "%s: %s" % (type(e).__name__, e)
            if body is not None:
                break
            if _attempt + 1 < max(1, attempts):
                time.sleep(0.4)
        if not body:
            _log("sources: dav details failed for %s (%s)" % (safe_label(url), last))
            return {}
        root = ET.fromstring(body)
    except Exception as e:
        _log("sources: dav details failed for %s: %s" % (safe_label(url), e))
        return {}
    # Prefix turning a server href into the child URL the listing uses: the
    # original "scheme://userinfo@host:port" verbatim (byte-for-byte match).
    try:
        scheme_part, rest_part = url.split("://", 1)
        prefix = scheme_part + "://" + rest_part.split("/", 1)[0]
    except Exception:
        prefix = "%s://%s" % (scheme, netloc)

    def _tag(el):
        return el.tag.rsplit("}", 1)[-1].lower()

    out = {}
    for resp in list(root):
        if _tag(resp) != "response":
            continue
        href, length, modified, is_dir = "", "", "", False
        for el in resp.iter():
            t = _tag(el)
            if t == "href" and not href:
                href = (el.text or "").strip()
            elif t == "getcontentlength":
                length = (el.text or "").strip()
            elif t == "getlastmodified":
                modified = (el.text or "").strip()
            elif t == "collection":
                is_dir = True
        if not href:
            continue
        mtime = 0
        if modified:
            try:
                mtime = int(parsedate_to_datetime(modified).timestamp())
            except Exception:
                mtime = 0
        size = 0
        if length.isdigit():
            size = int(length)
        info = (size, mtime, is_dir)
        out[prefix + href] = info
        name = href.rstrip("/").rsplit("/", 1)[-1]
        if name:
            out.setdefault(unquote(name), info)
    return out


def net_mtime(path):
    """Modification time (epoch seconds) of a network file, or 0.

    Uses xbmcvfs.Stat (PROPFIND-0); a direct urllib PROPFIND tends to 503."""
    try:
        if is_network_path(path):
            st = xbmcvfs.Stat(path)
            mt = int(st.st_mtime()) or int(st.st_ctime())
            if mt > 0:
                return mt
    except Exception:
        pass
    return 0


def url_unquote(text):
    """Percent-decode a network-URL text fragment for DISPLAY (see url_display)."""
    try:
        return unquote(text or "")
    except Exception:
        return text or ""


def net_parent(path):
    """Parent of a network URL, stopping AT the host. os.path.dirname yields
    garbage on a network URL, so never walk one with dirname. "" at host root."""
    p = rstrip_slash(path)
    head, sep, rest = p.partition("://")
    if not sep:
        return ""
    host, slash, sub = rest.partition("/")
    if not slash or not sub:
        return ""
    if "/" not in sub:
        return "%s://%s" % (head, host)
    return "%s://%s/%s" % (head, host, sub.rsplit("/", 1)[0])

NETSRC_ROWS = 10


def _netsrc_path():
    return _state_file("network-sources.json")


def netsrc_load():
    """Stored network sources ([{label, path, fields?}], order preserved) or [].
    Path may be empty; `fields` holds the editor values verbatim."""
    data = _read_json(_netsrc_path(), [])
    if not isinstance(data, list):
        return []
    out = []
    for s in data:
        if isinstance(s, dict) and s.get("label"):
            e = {"label": str(s["label"]),
                 "path": str(s.get("path") or "")}
            f = s.get("fields")
            if isinstance(f, dict):
                e["fields"] = {k: str(v or "") for k, v in f.items()
                               if k in ("scheme", "server", "port",
                                        "path", "user", "pass")}
            out.append(e)
    return out


def netsrc_valid(path):
    """True when the path uses a natively supported network scheme."""
    try:
        return (path or "").strip().lower().startswith(NET_SCHEMES)
    except Exception:
        return False


_NET_NOSPACE = {ord(c): None for c in " \t\r\n"}


def netsrc_sanitize(field, value):
    """Drop whitespace from server/port only (other fields may contain spaces)."""
    v = value or ""
    if field in ("server", "port"):
        v = v.translate(_NET_NOSPACE)
    return v


def netsrc_split(value):
    """(host, port, path) for a SERVER field value: drop a pasted scheme/
    credentials and split an embedded port/path out."""
    v = netsrc_sanitize("server", value)
    if "://" in v:
        v = v.split("://", 1)[1]
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    v, _, path = v.partition("/")
    port = ""
    if ":" in v:
        v, port = v.rsplit(":", 1)
    return v, port, path


def netsrc_host(path):
    """Display host of a network URL (strips scheme, credentials, port/path)."""
    try:
        rest = (path or "").strip()
        rest = rest.split("://", 1)[1] if "://" in rest else rest
        rest = rest.split("@")[-1]
        return rest.split("/")[0].split(":")[0]
    except Exception:
        return ""


def rstrip_slash(path):
    """Strip trailing "/" but never eat a scheme's "//": a bare "ftp://" is a
    valid half-filled entry and must survive rstrip("/")."""
    p = path or ""
    while p.endswith("/") and not p.endswith("://"):
        p = p[:-1]
    return p


def _netsrc_entry(label, path, fields):
    """Entry dict. `fields` keeps the editor values verbatim (the stored path
    cannot round-trip arbitrary input); path stays the assembled browsable URL."""
    entry = {"label": label, "path": path}
    if fields:
        clean = {k: (v or "") for k, v in fields.items()
                 if k in ("scheme", "server", "port", "path", "user", "pass")}
        if clean:
            entry["fields"] = clean
    return entry


def netsrc_add(label, path, fields=None):
    """Append a network source. Label required; path optional (validated when
    present). Duplicate non-empty paths rejected. Visible by default."""
    path = rstrip_slash((path or "").strip())
    label = (label or "").strip()
    if not label:
        return False
    if path and not netsrc_valid(path):
        return False
    cur = netsrc_load()
    if path and any((e.get("path") or "").lower() == path.lower() for e in cur):
        return False
    cur.append(_netsrc_entry(label, path, fields))
    from common import write_json as _write_json
    _write_json(_netsrc_path(), cur)
    return True


def netsrc_replace(idx, label, path, fields=None):
    """Replace the 1-based entry. Same rules as netsrc_add (this entry excluded
    from the duplicate check)."""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return False
    path = rstrip_slash((path or "").strip())
    label = (label or "").strip()
    if not label:
        return False
    if path and not netsrc_valid(path):
        return False
    cur = netsrc_load()
    if not (1 <= idx <= len(cur)):
        return False
    if path and any((e.get("path") or "").lower() == path.lower()
                    for j, e in enumerate(cur) if j != idx - 1):
        return False
    cur[idx - 1] = _netsrc_entry(label, path, fields)
    from common import write_json as _write_json
    _write_json(_netsrc_path(), cur)
    return True


def netsrc_remove(idx):
    """Remove the 1-based entry."""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return False
    cur = netsrc_load()
    if 1 <= idx <= len(cur):
        del cur[idx - 1]
        from common import write_json as _write_json
        _write_json(_netsrc_path(), cur)
        return True
    return False


def dirsource_drives():
    """User directory sources as quasi-drives (directory-sources.json). Non-
    existent paths are skipped; label truncated like the dropdown."""
    import json as _json
    out = []
    try:
        from common import read_text as _read_text
        txt = _read_text(_state_file("directory-sources.json"))
        if txt:
            data = _json.loads(txt)
            if isinstance(data, list):
                for idx, p in enumerate(data):
                    if isinstance(p, str) and p.strip():
                        path = p.strip().rstrip("/")
                        if not path:
                            continue
                        if not os.path.isdir(path):
                            continue
                        label = safe_label(os.path.basename(path.rstrip("/")) or path)
                        # Truncate like the dropdown.
                        if len(label) > 15:
                            label = label[:15].rstrip() + "…"
                        # `slot` = 1-based position in directory-sources.json
                        # (settings rows use this; visible() must not index the filtered list).
                        out.append({"label": label, "path": path,
                                    "type": "dirsource", "slot": idx + 1,
                                    "key": dirsource_key(path)})
    except Exception:
        pass
    return out


def drive_entries():
    """DRIVE entries only (home first, then local volumes); dirsource/network
    sources are merged live by readers, never duplicated here."""
    return [d for d in all_drives()
            if d.get("type") not in ("dirsource", "network")]


def all_drives():
    """FULL source list: home first, then local volumes, dirsources, network
    sources. hidden.drive.<identity> covers regular drives only."""
    drives = []
    home = home_drive()
    if home:
        drives.append(home)
    drives.extend(local_drives())
    drives.extend(dirsource_drives())
    drives.extend(network_sources())
    return drives


def label_for(path):
    """Display label for a source root (dropdown label, else basename)."""
    p = (path or "").rstrip("/")
    for d in all_drives():
        if (d.get("path") or "").rstrip("/") == p:
            return d.get("label") or os.path.basename(p) or p
    return os.path.basename(p) or p


def short_label(path):
    """Chip label: label_for() truncated at 35 chars; sanitized for properties."""
    s = _safe_label(label_for(path))
    return s[:35].rstrip() + "…" if len(s) > 35 else s


_MOUNT_BASES = ("/Volumes", "/run/media", "/media", "/var/media", "/mnt", "/storage")


def _strip_mount_base(path):
    """Strip a mount-base prefix for the display fallback (unplugged sources).
    /media and /run/media carry a user level; one segment is dropped there."""
    p = (path or "").rstrip("/")
    for base in _MOUNT_BASES:
        norm = base.rstrip("/")
        if p != norm and p.startswith(norm + "/"):
            segs = [s for s in p[len(norm):].strip("/").split("/") if s]
            if base in ("/media", "/run/media") and len(segs) > 1:
                segs = segs[1:]
            return "/".join(segs)
    return p


def display_path(path):
    """User display form of a path: starts at the longest matching drive root
    (dirsources never match themselves); sanitized for the property boundary."""
    disp = (path or "").rstrip("/")
    if not disp:
        return _safe_label("")
    try:
        candidates = [((d.get("path") or "").rstrip("/"), d.get("label") or "")
                      for d in all_drives()
                      if d.get("path") and d.get("type") != "dirsource"]
    except Exception:
        candidates = []
    best = None
    for root, label in candidates:
        if root and (disp == root or disp.startswith(root + "/")):
            if best is None or len(root) > len(best[0]):
                best = (root, label or os.path.basename(root) or root)
    if best:
        root, label = best
        rel = disp[len(root):].strip("/")
        if rel:
            return _safe_label(label + " / " + " / ".join(s for s in rel.split("/") if s))
        return _safe_label(label)
    disp = _strip_mount_base(disp)
    return _safe_label(" / ".join(s for s in disp.split("/") if s))


def display_path_below_source(folder):
    """Player path line form: starts at the longest non-dirsource drive root
    (dirsource paths resolve through their parent drive). Else display_path()."""
    p = (folder or "").rstrip("/")
    try:
        candidates = [((d.get("path") or "").rstrip("/"), d.get("label") or "")
                      for d in all_drives()
                      if d.get("path") and d.get("type") != "dirsource"]
    except Exception:
        candidates = []
    best = None
    for root, label in candidates:
        if p == root or p.startswith(root + "/"):
            if best is None or len(root) > len(best[0]):
                best = (root, label or os.path.basename(root) or root)
    if best:
        root, label = best
        rel = p[len(root):].strip("/")
        if rel:
            return _safe_label(label + " / " + " / ".join(s for s in rel.split("/") if s))
        return _safe_label(label)
    return display_path(p)


def drive_key(drive):
    """Stable identity of a regular drive: md5 of the normalized path (survives
    plug/unplug reorder; a volume rename needs a re-toggle)."""
    try:
        path = ((drive or {}).get("path") or "").rstrip("/")
    except Exception:
        path = ""
    return hashlib.md5(path.encode("utf-8", "replace")).hexdigest()


def dirsource_key(path):
    """Stable identity of a directory source: md5 of the normalized path."""
    p = (path or "").rstrip("/")
    return hashlib.md5(p.encode("utf-8", "replace")).hexdigest()


def drive_shown(drive):
    """True when the regular drive is not explicitly hidden (opt-out via
    hidden.drive.<identity>, default shown). Retired hide.drive.<identity> ignored."""
    try:
        return not hidden("hidden.drive.%s" % drive_key(drive))
    except Exception:
        return True


def netsrc_key(entry, idx=0):
    """Visibility identity of a network source: path hash, else a slot hash
    (two empty entries must not share a key or toggling one toggles all)."""
    path = (entry or {}).get("path") or ""
    if path:
        return dirsource_key(path)
    return hashlib.md5(("netsrc-slot:%d" % int(idx or 0)).encode("utf-8", "replace")).hexdigest()


def netsrc_shown(entry, idx=0):
    """True when the network source is not explicitly hidden."""
    try:
        return not hidden("hidden.drive.%s" % netsrc_key(entry, idx))
    except Exception:
        return True


def dirsource_hidden(ident):
    """True when hide.dirsource.<ident> is set (`ident` is the path hash)."""
    if not ident:
        return False
    try:
        return bool(xbmc.getCondVisibility("Skin.HasSetting(hide.dirsource.%s)" % ident))
    except Exception:
        return False


def hotplug_signature():
    """Cheap fingerprint of the pluggable device set for the hotplug watch."""
    try:
        entries = [(d.get("label", ""), d.get("path", "")) for d in local_drives()]
        home = home_drive()
        if home:
            entries.append((home.get("label", ""), home.get("path", "")))
        return repr(sorted(entries))
    except Exception:
        return ""


def opt_in(name):
    """True when an opt-in skin setting is set (show.system)."""
    try:
        return bool(xbmc.getCondVisibility("Skin.HasSetting(%s)" % name))
    except Exception:
        return False


def hidden(name):
    """True when an opt-out skin setting is set (hide.home)."""
    try:
        return bool(xbmc.getCondVisibility("Skin.HasSetting(%s)" % name))
    except Exception:
        return False


def drive_icon(drive):
    """Row icon texture for a source entry (house home, monitor system, USB
    local, network box share, folder dirsource)."""
    try:
        dtype = (drive or {}).get("type", "")
    except Exception:
        dtype = ""
    if dtype == "home":
        return "devices/house.png"
    if dtype == "dirsource":
        return "filetypes/folder.png"
    if dtype == "network":
        return "devices/hdd-network.png"
    try:
        if (drive or {}).get("system"):
            return "devices/pc-display.png"
    except Exception:
        pass
    return "devices/usb-drive.png"


def icon_for(path):
    """Icon texture for the entered source root (longest matching root wins,
    dirsources included). "" when nothing matches."""
    p = (path or "").rstrip("/")
    if not p:
        return ""
    try:
        roots = [((d.get("path") or "").rstrip("/"), d)
                 for d in all_drives() if d.get("path")]
    except Exception:
        return ""
    best = None
    for root, d in roots:
        if root and (p == root or p.startswith(root + "/")):
            if best is None or len(root) > len(best[0]):
                best = (root, d)
    if not best:
        return ""
    return drive_icon(best[1])


def local_regular(drives):
    """Regular local drives (no home/system/dirsource/network), enumeration
    order. Settings drive rows cover locals only."""

    def regular(d):
        return d.get("type") not in ("dirsource", "home", "network") and not d.get("system")
    return [d for d in drives if regular(d)]


def visible(drives):
    """ACTIVE drives in dropdown order: home, locals (opt-out hidden.drive.
    <identity>, compacted), system, dirsources, network. Home/system use no slot."""

    local = local_regular(drives)
    net = [d for d in drives if d.get("type") == "network"]
    dirsources = [d for d in drives if d.get("type") == "dirsource"]
    vis = []
    if not hidden("hide.home"):
        vis.extend([d for d in drives if d.get("type") == "home"])
    for d in local:
        if drive_shown(d):
            vis.append(d)
    if opt_in("show.system"):
        vis.extend([d for d in drives if d.get("system")])
    for i, d in enumerate(dirsources):
        # Pass the path hash, never the index, so hidden stays across removals.
        if not dirsource_hidden(d.get("key")):
            vis.append(d)
    for d in net:
        # netsrc_key carries the per-entry identity (path hash, else slot hash)
        ident = d.get("key") or drive_key(d)
        if not hidden("hidden.drive.%s" % ident):
            vis.append(d)
    return vis


def next_visible_after(cur_root, drives, visible):
    """Next active drive after `cur_root` in the full drive order (wraps).
    Used when the browsed drive is hidden live."""
    vis = {(d.get("path") or "").rstrip("/") for d in visible}
    order = [(d.get("path") or "").rstrip("/") for d in drives]
    try:
        pos = order.index(cur_root)
    except ValueError:
        return None
    for i in list(range(pos + 1, len(order))) + list(range(0, pos + 1)):
        if order[i] in vis:
            return drives[i]
    return None

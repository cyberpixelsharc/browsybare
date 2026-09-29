#!/usr/bin/env python3
"""Sources/drives data layer (no skin UI; main.py orchestrates the state).
drive-sources.json holds DRIVE entries only; other sources merge live."""
import hashlib
import os
import time
import sys
from urllib.parse import unquote, quote

import xbmc
import xbmcvfs

from common import (read_json as _read_json, state_file as _state_file,
                    safe_label as safe_label, redact, log as _log)
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
            f = s.get("fields") if isinstance(s.get("fields"), dict) else None
            path = netsrc_path_from_fields(f) if f else str(s.get("path") or "")
            wa = _netsrc_writeaccess(s)
            entry = {"label": str(s["label"]), "path": path, "writeaccess": wa}
            # Drop non-network non-empty paths (they would list empty as local);
            # path-less entries stay (display/toggle only).
            if entry["path"] and not is_network_path(entry["path"]):
                continue
            out.append({"label": entry["label"], "path": entry["path"],
                        "writeaccess": wa,
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
    """True for network schemes without write support (ftp/ftps) or for a
    network source that is flagged read-only (the per-source default)."""
    p = path or ""
    try:
        if p.lower().startswith(RO_SCHEMES):
            return True
    except Exception:
        return False
    try:
        pl = p.lower()
        for s in network_sources():
            sp = (s.get("path") or "").lower()
            if not sp:
                continue
            if pl == sp or pl.startswith(sp.rstrip("/") + "/"):
                return not bool(s.get("writeaccess", False))
    except Exception:
        pass
    return False


def url_display(path):
    """Human-readable path for DISPLAY only: network URLs stay percent-encoded
    for Kodi's VFS, only displayed text is decoded. Local paths pass through."""
    try:
        return unquote(path) if is_network_path(path) else path
    except Exception:
        return path


def _auth_params(challenge):
    """Parse a WWW-Authenticate parameter list (key=value / key="value") into a
    lower-cased dict."""
    out = {}
    i = 0
    n = len(challenge or "")
    while i < n:
        while i < n and challenge[i] in " ,":
            i += 1
        j = challenge.find("=", i)
        if j < 0:
            break
        key = challenge[i:j].strip().lower()
        k = j + 1
        if k < n and challenge[k] == '"':
            m = challenge.find('"', k + 1)
            if m < 0:
                break
            out[key] = challenge[k + 1:m]
            i = m + 1
        else:
            m = k
            while m < n and challenge[m] != ",":
                m += 1
            out[key] = challenge[k:m].strip()
            i = m
    return out


def _auth_quote(value):
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def _dav_origin(url):
    """Normalized DAV HTTP origin, URL path, and decoded credentials."""
    try:
        from urllib.parse import unquote, urlsplit
    except Exception:
        return None
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme == "dav":
            scheme = "http"
        elif scheme == "davs":
            scheme = "https"
        elif scheme not in ("http", "https"):
            return None
        host = (parsed.hostname or "").lower()
        if not host or not parsed.path.startswith("/"):
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
        bracketed = "[%s]" % host if ":" in host else host
        netloc = bracketed + (":%d" % parsed.port if parsed.port else "")
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        username = unquote(parsed.username) if parsed.username is not None else None
        password = unquote(parsed.password or "")
        return (scheme, host, port, netloc, path, username, password)
    except (TypeError, ValueError):
        return None


def dav_copy_file(src_url, dst_url, timeout=60):
    """Server-side copy for one file whose URLs share a DAV origin.

    Returns (True, "") when accepted, otherwise (False, reason). CloudMe only
    accepts the COPY Destination as a path, so try the standard absolute URI
    before the path form."""
    try:
        import ssl
        import urllib.error
        import urllib.request
        import xml.etree.ElementTree as ET
    except Exception:
        return False, "unsupported"
    try:
        src = _dav_origin(src_url)
        dst = _dav_origin(dst_url)
    except Exception:
        return False, "unsupported"
    if src is None or dst is None:
        return False, "unsupported"
    if src[:3] != dst[:3] or src[5:] != dst[5:]:
        return False, "unsupported"
    if src_url.endswith("/"):
        return False, "unsupported"
    scheme, _host, _port, netloc, src_path, username, password = src
    dst_path = dst[4]
    absolute_destination = "%s://%s%s" % (scheme, netloc, dst_path)
    for destination in (absolute_destination, dst_path):
        auth_header = ""
        challenges = 0
        last = ""
        while True:
            try:
                req = urllib.request.Request(
                    "%s://%s%s" % (scheme, netloc, src_path), method="COPY")
                req.add_header("Destination", destination)
                req.add_header("Overwrite", "F")
                if auth_header:
                    req.add_header("Authorization", auth_header)
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=ssl.create_default_context()) as resp:
                    try:
                        status = resp.getcode()
                    except Exception:
                        status = getattr(resp, "status", 0) or 0
                    if status in (200, 201, 204):
                        return True, ""
                    if status == 207:
                        try:
                            body = resp.read(65536)
                            root = ET.fromstring(body)
                            statuses = [el.text or "" for el in root.iter()
                                        if el.tag.rsplit("}", 1)[-1].lower() == "status"]
                        except Exception:
                            return False, "unexpected"
                        if statuses and all(s.split(" ", 2)[1].startswith("2")
                                            for s in statuses):
                            return True, ""
                        return False, "unexpected"
                    if status == 412:
                        return False, "exists"
                    last = "status-%s" % status
            except urllib.error.HTTPError as err:
                if err.code == 401 and username is not None and challenges < 1:
                    challenge = (err.headers.get("WWW-Authenticate", "")
                                 if err.headers else "") or ""
                    new_auth = _dav_auth_header("COPY", src_path, username,
                                                password, challenge)
                    if new_auth and new_auth != auth_header:
                        auth_header = new_auth
                        challenges += 1
                        continue
                    return False, "auth"
                if err.code == 412:
                    return False, "exists"
                last = "status-%s" % err.code
            except Exception as err:
                last = "%s: %s" % (type(err).__name__, err)
            break
    return False, last


def _dav_auth_header(method, uri, username, password, challenge):
    """Authorization header answering a WWW-Authenticate challenge: Digest (MD5
    or MD5-sess, qop=auth) or Basic. Empty when neither is offered."""
    import base64
    import hashlib
    import os as _os
    challenge = (challenge or "").strip()
    if not challenge:
        return ""
    scheme = challenge.split(" ", 1)[0].lower()
    if scheme == "basic":
        token = base64.b64encode(
            ("%s:%s" % (username or "", password or "")).encode("utf-8")).decode("ascii")
        return "Basic " + token
    if scheme != "digest":
        return ""
    params = _auth_params(challenge.split(" ", 1)[1])
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    if not nonce:
        return ""
    qop = params.get("qop", "")
    opaque = params.get("opaque", "")
    algorithm = params.get("algorithm", "MD5")
    cnonce = _os.urandom(8).hex()
    nc = "00000001"
    ha1 = hashlib.md5(("%s:%s:%s" % (
        username or "", realm, password or "")).encode("utf-8")).hexdigest()
    if algorithm.upper() == "MD5-SESS":
        ha1 = hashlib.md5(("%s:%s:%s" % (ha1, nonce, cnonce)).encode("utf-8")).hexdigest()
    ha2 = hashlib.md5(("%s:%s" % (method, uri)).encode("utf-8")).hexdigest()
    qop_sel = ""
    if qop:
        offered = [q.strip().lower() for q in qop.split(",") if q.strip()]
        qop_sel = "auth" if "auth" in offered else (offered[0] if offered else "")
    if qop_sel:
        digest = hashlib.md5(("%s:%s:%s:%s:%s:%s" % (
            ha1, nonce, nc, cnonce, qop_sel, ha2)).encode("utf-8")).hexdigest()
    else:
        digest = hashlib.md5(("%s:%s:%s" % (ha1, nonce, ha2)).encode("utf-8")).hexdigest()
    parts = ['username="%s"' % _auth_quote(username), 'realm="%s"' % _auth_quote(realm),
             'nonce="%s"' % nonce, 'uri="%s"' % uri, 'response="%s"' % digest]
    if qop_sel:
        parts += ['qop=%s' % qop_sel, 'nc=%s' % nc, 'cnonce="%s"' % cnonce]
    if opaque:
        parts.append('opaque="%s"' % opaque)
    if algorithm:
        parts.append('algorithm=%s' % algorithm)
    return "Digest " + ", ".join(parts)


def _src_size(path):
    """Byte size of a local file / VFS URL, or None when unavailable."""
    try:
        if is_network_path(path):
            st = xbmcvfs.Stat(path)
            size = (st.st_size() if callable(getattr(st, "st_size", None))
                    else st.st_size)
            return int(size)
        return os.path.getsize(path)
    except Exception:
        return None


def _local_source(src_path):
    """A local readable copy of `src_path`: the path itself when local, else a
    downloaded temp file (Kodi's `xbmcvfs.File.read` decodes as text and raises
    on binary content, so a network source is pulled to disk first). Returns
    (local_path, temp_path_or_"") or (None, reason)."""
    if not is_network_path(src_path):
        return src_path, ""
    try:
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(),
                           "bp-upload-%s" % os.urandom(6).hex())
    except Exception:
        return None, "temp unavailable"
    # The source download hits the same transient 5xx as everything else (a
    # flaky CloudMe 502s); retry a few times before giving up.
    for attempt in range(3):
        try:
            if xbmcvfs.copy(src_path, tmp):
                return tmp, tmp
        except Exception:
            pass
        if attempt < 2:
            time.sleep(1.0)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return None, "source download failed"


def dav_upload_file(src_path, dst_url, timeout=300, on_progress=None, cancelled=None):
    """Upload one file to a WebDAV URL with our own streaming PUT.

    Kodi's VFS write to DAV fails (curl cannot rewind the upload body after the
    401 challenge), so a local->network upload is impossible through
    `xbmcvfs.copy`. We fetch the challenge first, then PUT with the body in
    chunks -- no memory copy, no rewind needed. A network `src_path` is pulled
    to a temp file first (Kodi's `xbmcvfs.File.read` decodes as text and raises
    on binary content). `on_progress(sent, total)` runs while streaming,
    `cancelled()` (optional) aborts. Returns (True, "") or (False, reason)."""
    try:
        import http.client
        import ssl
        import urllib.error
        import urllib.request
    except Exception:
        return False, "unsupported"
    dst = _dav_origin(dst_url)
    if dst is None:
        return False, "unsupported"
    src_local, tmp = _local_source(src_path)
    if src_local is None:
        return False, tmp or "source unreadable"
    try:
        scheme, host, port, netloc, path, username, password = dst
        total = _src_size(src_local) or 0
        # Auth: prefer the server's challenge (unauthenticated Depth-0
        # PROPFIND, a few tries -- a flaky server answers 5xx); fall back to
        # PREEMPTIVE Basic so a failed probe never sends us in unauthenticated.
        # A 401 on the PUT itself is answered once more below.
        challenge = ""
        for _ in range(3):
            try:
                req = urllib.request.Request(
                    "%s://%s%s" % (scheme, netloc, path), method="PROPFIND")
                req.add_header("Depth", "0")
                urllib.request.urlopen(req, timeout=min(timeout, 20),
                                       context=ssl.create_default_context())
                break
            except urllib.error.HTTPError as e:
                if e.code == 401 and e.headers:
                    challenge = e.headers.get("WWW-Authenticate", "") or ""
                    break
            except Exception:
                pass
        auth = _dav_auth_header("PUT", path, username, password,
                                challenge or "Basic")

        def new_conn():
            if scheme == "https":
                return http.client.HTTPSConnection(
                    host, port, timeout=timeout,
                    context=ssl.create_default_context())
            return http.client.HTTPConnection(host, port, timeout=timeout)

        try:
            conn = new_conn()
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e)
        try:
            for attempt in range(2):
                sent = 0
                conn.putrequest("PUT", path)
                conn.putheader("Content-Length", str(total))
                conn.putheader("Content-Type", "application/octet-stream")
                if auth:
                    conn.putheader("Authorization", auth)
                conn.endheaders()
                with open(src_local, "rb") as f:
                    while True:
                        if cancelled is not None and cancelled():
                            return False, "cancelled"
                        chunk = f.read(262144)
                        if not chunk:
                            break
                        conn.send(chunk)
                        sent += len(chunk)
                        if on_progress is not None:
                            try:
                                on_progress(sent, total)
                            except Exception:
                                pass
                resp = conn.getresponse()
                status = int(getattr(resp, "status", 0) or 0)
                ch = (resp.headers.get("WWW-Authenticate", "")
                      if resp.headers else "") or ""
                try:
                    resp.read(1024)
                except Exception:
                    pass
                if status in (200, 201, 204):
                    return True, ""
                if status == 401 and attempt == 0:
                    new_auth = _dav_auth_header("PUT", path, username,
                                                password, ch)
                    if new_auth and new_auth != auth:
                        auth = new_auth
                        try:
                            conn.close()
                        except Exception:
                            pass
                        try:
                            conn = new_conn()
                        except Exception:
                            return False, "auth"
                        continue
                if status == 401:
                    return False, "auth"
                return False, "status-%d" % status
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def dav_details(url, timeout=8, attempts=2):
    """{key: (size, mtime, is_dir)} for a WebDAV collection via ONE PROPFIND
    (Depth 1). Keys are the full child URL and the decoded name; {} on failure."""
    try:
        import ssl
        import urllib.error
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
        username = unquote(u.username) if u.username is not None else None
        password = unquote(u.password or "")
        body = None
        last = ""
        auth_header = ""
        handshake = 0
        tries = max(1, attempts)
        attempt = 0
        while attempt < tries:
            attempt += 1
            try:
                req = urllib.request.Request(request_url, method="PROPFIND")
                req.add_header("Depth", "1")
                req.add_header("Content-Type", "text/xml; charset=utf-8")
                if auth_header:
                    req.add_header("Authorization", auth_header)
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=ssl.create_default_context()) as r:
                    body = r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 401 and handshake < 2 and username is not None:
                    # Answer the WWW-Authenticate challenge (Digest or Basic);
                    # the auth handshake itself is not a network retry.
                    handshake += 1
                    challenge = (e.headers.get("WWW-Authenticate", "")
                                 if e.headers else "") or ""
                    new_auth = _dav_auth_header("PROPFIND", path, username,
                                                password, challenge)
                    if new_auth and new_auth != auth_header:
                        auth_header = new_auth
                        attempt -= 1
                        continue
                    last = "401 (%s)" % (challenge.split(" ", 1)[0] or "no auth")
                else:
                    last = "HTTP %s" % e.code
            except ssl.SSLError as e:
                # NO silent TLS downgrade: the request carries credentials, so a
                # verification bypass would defeat MITM protection. Treat as failure.
                last = "%s: %s" % (type(e).__name__, e)
            except Exception as e:
                # a 503 from an overloaded server is transient: retry
                last = "%s: %s" % (type(e).__name__, e)
            if attempt < tries:
                time.sleep(0.4)
        if not body:
            _log("sources: dav details failed for %s (%s)" % (redact(url), last))
            return {}
        root = ET.fromstring(body)
    except Exception as e:
        _log("sources: dav details failed for %s: %s" % (redact(url), e))
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
    """Stored network sources ([{label, path, writeaccess, fields?}], order
    preserved) or []. The browsable `path` is derived live from `fields` when
    present; a legacy entry without fields keeps its stored path."""
    data = _read_json(_netsrc_path(), [])
    if not isinstance(data, list):
        return []
    out = []
    for s in data:
        if isinstance(s, dict) and s.get("label"):
            f = s.get("fields") if isinstance(s.get("fields"), dict) else None
            e = {"label": str(s["label"]),
                 "path": netsrc_path_from_fields(f) if f else str(s.get("path") or ""),
                 "writeaccess": _netsrc_writeaccess(s)}
            if f:
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


def netsrc_url(scheme, server, port, path, user, passwd):
    """Assemble the browsable VFS URL from the editor fields. Empty when there
    is no server. The userinfo is percent-encoded: Kodi parses davs as https,
    so a "?;#|" in a password would be the option separator and an "@" would end
    the userinfo early; Kodi decodes %XX in user/password (CURL::Parse)."""
    scheme = (scheme or "ftp").split("://", 1)[0].lower() or "ftp"
    server, s_port, s_path = netsrc_split(server)
    port = netsrc_sanitize("port", port).strip() or s_port
    path = (path or "").strip().strip("/") or s_path.strip("/")
    user = (user or "").strip()
    if not server:
        return ""
    user_enc = quote(user, safe="")
    pass_enc = quote(passwd or "", safe="")
    creds = (user_enc + ((":" + pass_enc) if pass_enc else "") + "@") if user_enc else ""
    srv = server[:-1] if (server.endswith("/")
                          and not server.endswith("://")) else server
    url = scheme + "://" + creds + srv
    if port.isdigit():
        url += ":" + port
    if path:
        url += "/" + path
    return url if netsrc_valid(url) else ""


def netsrc_path_from_fields(fields):
    """Browsable path derived live from the editor fields ('' when no address).
    The stored 'path' is optional -- the fields are the source of truth."""
    f = fields or {}
    return netsrc_url(f.get("scheme", "ftp"), f.get("server", ""),
                      f.get("port", ""), f.get("path", ""),
                      f.get("user", ""), f.get("pass", ""))


def _netsrc_writeaccess(s):
    """Write-access flag of a stored entry. Migrates the legacy inverted
    'readonly' key; default False (read-only)."""
    if not isinstance(s, dict):
        return False
    if "writeaccess" in s:
        return bool(s.get("writeaccess"))
    if "readonly" in s:
        return not bool(s.get("readonly"))
    return False


def _netsrc_entry(label, path, fields, writeaccess=False):
    """Entry dict. The editor `fields` are the source of truth; the browsable
    `path` is derived from them on load and stored only for legacy/path-only
    entries. `writeaccess` defaults to False (read-only)."""
    entry = {"label": label, "writeaccess": bool(writeaccess)}
    if fields:
        clean = {k: (v or "") for k, v in fields.items()
                 if k in ("scheme", "server", "port", "path", "user", "pass")}
        if clean:
            entry["fields"] = clean
    if "fields" not in entry and path:
        entry["path"] = path
    return entry


def netsrc_add(label, path, fields=None, writeaccess=False):
    """Append a network source. Label required; path optional (validated when
    present). Duplicate non-empty paths rejected. Visible by default.
    Read-only by default (writes must be enabled)."""
    path = rstrip_slash((path or "").strip())
    label = (label or "").strip()
    if not label:
        return False
    if path and not netsrc_valid(path):
        return False
    cur = netsrc_load()
    if path and any((e.get("path") or "").lower() == path.lower() for e in cur):
        return False
    cur.append(_netsrc_entry(label, path, fields, writeaccess))
    from common import write_json as _write_json
    _write_json(_netsrc_path(), cur)
    return True


def netsrc_replace(idx, label, path, fields=None, writeaccess=False):
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
    cur[idx - 1] = _netsrc_entry(label, path, fields, writeaccess)
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
    vis = {rstrip_slash(d.get("path") or "") for d in visible}
    order = [rstrip_slash(d.get("path") or "") for d in drives]
    try:
        pos = order.index(cur_root)
    except ValueError:
        return None
    for i in list(range(pos + 1, len(order))) + list(range(0, pos + 1)):
        if order[i] in vis:
            return drives[i]
    return None

#!/usr/bin/env python3
"""Shared toolbox for all Browsybare scripts (log/localize/dialog flags/IO). Everything points HERE, never back to main.py, keeping the import graph acyclic."""
import json
import hashlib
import os
import re
import tempfile
import time
from urllib.parse import quote, unquote_to_bytes

import xbmc
import xbmcgui
import xbmcvfs

LOG = "[skin] "
# Percent-encoded userinfo ("%3A%2F%2Fuser%3Apw%40host"): used by mask_creds.
_ENCODED_CREDS = re.compile(r"(?i)(%3a%2f%2f)([^%]+)(%3a)([^%]+)(%40)")

# log() replaces paths and file names with a stable short-hash marker so
# kodi.log never carries plaintext user paths. URLs and absolute paths are
# unambiguous; file names are heuristic (a token run ending in a known
# extension -- started by a capital/digit for multi-word names, so it does not
# swallow a lowercase duty prefix).
_EXT = (r"mp4|mkv|avi|mov|m4v|mpg|mpeg|webm|flv|wmv|ts|m2ts|ogv|3gp|"
        r"mp3|flac|m4a|aac|ogg|opus|wav|aiff|wma|ape|"
        r"jpg|jpeg|png|gif|bmp|tiff|tif|webp|heic|heif|avif|"
        r"srt|ass|ssa|sub|vtt|zip|rar|7z|tar|gz|iso|"
        r"pdf|txt|epub|mobi|doc|docx|xls|xlsx|ppt|pptx")
_PATH_MASK = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>|]+|(?<![\w])/[^\s'\"<>|]+")
_FILE_MASK = re.compile(
    r"(?<![\w])(?:[A-Z0-9][A-Za-z0-9_()\[\]&,+\-]*"
    r"(?: [A-Za-z0-9_()\[\]&,+\-]+){0,10}"
    r"|[A-Za-z0-9_()\[\]&,+\-][A-Za-z0-9_.()\[\]&,+\-]*)"
    r"\.(?i:" + _EXT + r")\b")


def _short_hash(text):
    try:
        return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    except Exception:
        return "?"


def mask_paths(s):
    """Redact URLs, absolute paths and file names from a log message."""
    try:
        s = _PATH_MASK.sub(lambda m: "[path:%s]" % _short_hash(m.group(0)), s or "")
        s = _FILE_MASK.sub(lambda m: "[file:%s]" % _short_hash(m.group(0)), s)
        return s
    except Exception:
        return s


def redact(value):
    """Stable '[file:<8hex>]' marker for a path/name in a log line, so callers
    never emit a basename. Same hash family as the resume store keys."""
    try:
        raw = cache_key(value or "")
        return "[file:%s]" % hashlib.sha256(
            raw.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    except Exception:
        return "[file]"


def mask_creds(s):
    """Hide the password of inline network URLs ("user:secret@host" -> "user:***@host"); log() runs this over every message so printed paths cannot leak credentials. Handles several URLs and passwords containing ":" or "@"."""
    s = s or ""
    low = s.lower()
    has_literal = "://" in s and "@" in s
    has_encoded = "%3a%2f%2f" in low and "%40" in low
    if not has_literal and not has_encoded:
        return s
    out = []
    try:
        while True:
            i = s.find("://")
            if i < 0:
                out.append(s)
                break
            out.append(s[:i + 3])
            s = s[i + 3:]
            end = len(s)
            for stop in ("/", " ", ")", "'", '"', "\n", "\t"):
                j = s.find(stop)
                if j >= 0:
                    end = min(end, j)
            at = s.rfind("@", 0, end)
            if at < 0:
                continue  # no userinfo here, keep scanning
            user = s[:at].split(":", 1)[0]
            out.append("%s:***@" % user)
            s = s[at + 1:]
    except Exception:
        return "".join(out) + s
    # Percent-encoded userinfo would leak; mask it via _ENCODED_CREDS.
    try:
        return _ENCODED_CREDS.sub(
            lambda m: m.group(1) + m.group(2) + m.group(3) + "***" + m.group(5),
            "".join(out))
    except Exception:
        return "".join(out)


def log(msg):
    # Sanitized: raw paths may carry surrogates the logging binding rejects;
    # strip CR/LF (log forging), mask network credentials and plaintext paths.
    try:
        s = safe_label(msg).replace("\r", "\\r").replace("\n", "\\n")
        xbmc.log(LOG + mask_paths(mask_creds(s)), xbmc.LOGINFO)
    except Exception:
        try:
            print(msg)
        except Exception:
            pass


def focus_control(control_id, tries=12, interval=0.05):
    """Focus a control as soon as its overlay becomes visible.

    Right after an overlay's `<visible>` property is set, a single SetFocus can
    land BEHIND it (visibility is evaluated a frame later), which used to force
    a fixed `time.sleep(0.3)` before every open -- a visible delay. Retry fast
    instead and stop as soon as Control.HasFocus confirms the focus landed.
    Returns True when the control holds focus."""
    try:
        cid = int(control_id)
    except (TypeError, ValueError):
        return False
    for _ in range(max(1, tries)):
        try:
            xbmc.executebuiltin("SetFocus(%d)" % cid)
            if xbmc.getCondVisibility("Control.HasFocus(%d)" % cid):
                return True
        except Exception:
            return False
        time.sleep(interval)
    return False


def skin_name():
    try:
        with open(os.path.join(skin_root(), "addon.xml"), "r", encoding="utf-8") as f:
            head = f.read(2000)
        m = re.search(r'<addon[^>]*?\sname="([^"]+)"', head)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "Skin"


def fail(msg):
    log("ERROR: %s" % msg)
    win = xbmcgui.Window(10000)
    win.setProperty("bp.status", "error")
    win.setProperty("bp.error", msg)
    try:
        xbmcgui.Dialog().ok(skin_name(), msg)
    except Exception:
        pass


def L(sid):
    """Localized skin string (31xxx) -- same lookup as $LOCALIZE in XML (g_localizeStrings)."""
    return xbmc.getLocalizedString(sid)


def ok():
    win = xbmcgui.Window(10000)
    win.clearProperty("bp.error")
    win.setProperty("bp.status", "ok")


def skin_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def state_dir():
    d = xbmcvfs.translatePath("special://userdata/skindev")
    os.makedirs(d, exist_ok=True)
    return d


# Legacy runtime file names (old -> new); state_file() renames in place once
# so no user data (toggles, sources) is lost. New code passes the NEW name.
STATE_LEGACY = {
    "drive-sources.json": ("drives.json",),
    "directory-sources.json": ("dirsources.json",),
    "network-sources.json": ("sources.json",),
    "blacklist-off.json": ("blacklist_off.json",),
    "folder-sizes.json": ("folder_sizes.json",),
}


def cache_key(path):
    """Credential-free cache key for a path: drops only the userinfo (scheme://host/path stays, so same-named files on different servers keep distinct keys). Local paths pass through byte-identical."""
    try:
        p = path or ""
        i = p.find("://")
        if i < 0:
            return path
        rest = p[i + 3:]
        slash = rest.find("/")
        at = rest.rfind("@", 0, slash if slash >= 0 else len(rest))
        if at < 0:
            return path
        return p[:i + 3] + rest[at + 1:]
    except Exception:
        return path


def state_file(name):
    """Absolute runtime file path for `name` (new naming), migrating a legacy file in place via atomic rename (readers never see a half-written file)."""
    d = state_dir()
    new = os.path.join(d, name)
    if not os.path.exists(new):
        for legacy in STATE_LEGACY.get(name, ()):
            old = os.path.join(d, legacy)
            if os.path.exists(old):
                try:
                    os.replace(old, new)
                    log("state: migrated %s -> %s" % (legacy, name))
                except OSError as e:
                    log("state: migration failed %s -> %s: %s" % (legacy, name, e))
                break
    return new


# Network folder listings shared between the browser (list.py) and the player
# (fileops.py): starting a file must not re-list a slow network folder -- Kodi's
# FTPS VFS re-handshakes TLS per call and can take minutes, while the browser has
# just listed the same folder. Short TTL bounds staleness; credential-free keys.
LISTING_CACHE = "folder-lists.json"
LISTING_TTL = 300.0
LISTING_MAX = 50


def listing_cache_path():
    try:
        return state_file(LISTING_CACHE)
    except Exception:
        return ""


def listing_get(folder, ttl=None):
    """(dirs, files) child names of a network `folder` from the shared cache, or
    None. `ttl` overrides the freshness window (0/negative = any age, used by
    the browser for stale-while-revalidate)."""
    path = listing_cache_path()
    if not path:
        return None
    data = read_json(path, {})
    e = data.get(cache_key((folder or "").rstrip("/"))) if isinstance(data, dict) else None
    if not isinstance(e, dict):
        return None
    window = LISTING_TTL if ttl is None else ttl
    if window > 0 and (time.time() - e.get("t", 0)) >= window:
        return None   # ttl<=0 means "any age" (browser stale-while-revalidate)
    dirs, files = e.get("dirs"), e.get("files")
    if isinstance(dirs, list) and isinstance(files, list):
        details = e.get("details")
        return dirs, files, details if isinstance(details, dict) else {}
    return None


def listing_put(folder, dirs, files, details=None):
    """Remember the child names (and their sizes/dates) of a network `folder`
    (bounded, newest kept). `details` is name -> [size, mtime, is_dir]."""
    path = listing_cache_path()
    if not path:
        return
    try:
        data = read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        entry = {"t": time.time(), "dirs": list(dirs), "files": list(files)}
        if details:
            entry["details"] = {k: list(v) for k, v in details.items()}
        data[cache_key((folder or "").rstrip("/"))] = entry
        if len(data) > LISTING_MAX:
            for k in sorted(data, key=lambda k: data[k].get("t", 0))[:-LISTING_MAX]:
                data.pop(k, None)
        write_json(path, data)
    except Exception:
        pass


def safe_label(s):
    """Kodi-safe display string: plain str, no surrogates (undecodable filenames segfault Kodi's Python bindings). Original bytes are guessed back (UTF-8 then cp1252); navigation paths stay raw -- display only."""
    try:
        if not isinstance(s, str):
            s = str(s)
        try:
            raw = s.encode("utf-8", "surrogateescape")
        except Exception:
            return s.encode("utf-8", "replace").decode("utf-8")
        for enc in ("utf-8", "cp1252"):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return raw.decode("utf-8", "replace")
    except Exception:
        return "?"


def safe_key(s):
    """Deterministic fingerprint for matching a mangled arrival path to its stored entry: surrogates, literal ? and U+FFFD all fold to ?. Display uses safe_label."""
    try:
        if not isinstance(s, str):
            s = str(s)
        return s.replace("�", "?").encode("utf-8", "replace").decode("utf-8")
    except Exception:
        return "?"


def path_enc(p):
    """Filesystem path -> pure-ASCII property storage (percent-encoded): window properties cannot carry surrogateescape names. path_dec() restores the exact bytes; display-only labels use safe_label."""
    try:
        return quote(p or "", safe="/", errors="surrogateescape")
    except Exception:
        return p or ""


def path_dec(s):
    """Inverse of path_enc: decodes to BYTES then os.fsdecode (fs encoding + surrogateescape), never straight to real chars -- Kodi's embedded Python runs without UTF-8 mode, so listdir speaks surrogate-escaped."""
    try:
        return os.fsdecode(unquote_to_bytes(s or ""))
    except Exception:
        return s or ""


def play_str(p):
    """Path form for Kodi C++ boundaries (playback): real UTF-8 chars (the
    Python-os form may be surrogate-escaped, which the bindings reject).
    Undecodable bytes (e.g. a Latin-1 FTP name) are percent-encoded -- never left
    as a surrogate, which segfaults the Kodi binding."""
    try:
        return os.fsencode(p).decode("utf-8")
    except Exception:
        out = []
        for ch in p:
            o = ord(ch)
            if 0xD800 <= o <= 0xDFFF:
                out.append("%%%02X" % (o - 0xDC00))   # surrogateescape byte
            else:
                out.append(ch)
        return "".join(out)


def sort_fold(s):
    """Sort key matching the displayed order across charsets: NFKD-folded, diacritics stripped, lowercase (ö->o, ß->ss, é->e, ñ->n). Display (safe_label) is untouched."""
    import unicodedata
    try:
        t = unicodedata.normalize("NFKD", s if isinstance(s, str) else str(s))
        t = "".join(c for c in t if unicodedata.category(c) != "Mn")
        return t.replace("ß", "ss").replace("ẞ", "SS").lower()
    except Exception:
        try:
            return s.lower()
        except Exception:
            return ""


def natkey(s):
    """Natural sort key over the folded display form: digit runs compare numerically ("Kapitel 2" before "Kapitel 10"). Structure is always [str, int, str, ...] (isdecimal-guarded), safe as a total order."""
    try:
        return [int(t) if t.isdecimal() else t
                for t in re.split(r"(\d+)", sort_fold(s))]
    except Exception:
        try:
            return [sort_fold(s)]
        except Exception:
            return [""]


def fs_path(path):
    """Return the path with the basename as the filesystem actually stores it (exFAT stores NFD while Kodi uses NFC and cannot open it; os.path.exists resolves both, so compare against the real listdir entries)."""
    if not path:
        return path
    directory, name = os.path.split(path)
    if not directory or not name:
        return path
    try:
        entries = os.listdir(directory)
    except OSError:
        return path
    if name in entries:
        return path
    import unicodedata
    for entry in entries:
        if unicodedata.normalize("NFC", entry) == unicodedata.normalize("NFC", name):
            log("fs_path: resolved special-case name (NFC/NFD): %s" % redact(name[-40:]))
            return os.path.join(directory, entry)
    return path


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    # ensure_ascii escapes lone surrogates in path keys instead of raising.
    # Unique tmp per WRITE: all RunScripts share one process (same pid).
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=directory)
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)  # fdopen failed: close the raw fd, else it leaks
            raise
        with f:
            json.dump(data, f, ensure_ascii=True, indent=1)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def read_text(path):
    """utf-8 read; None on ANY error -- callers self-heal (a truncated file must not crash the whole sync)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def write_text(path, s):
    """ATOMIC utf-8 write (unique tmp + replace): a cut multibyte char from an interrupted write would make the file undecodable. mkstemp avoids the same concurrent-writer race as write_json."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=directory)
    try:
        try:
            f = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)  # fdopen failed: close the raw fd, else it leaks
            raise
        with f:
            f.write(s)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def padding_step(is_pad, idx, last_idx, pushed_idx):
    """One skip decision for invisible list padding rows (list.py pads the full-height list with spacer items, and the core offers no non-selectable plugin item, so plain arrow navigation parks focus on them). Returns (action, last_idx, pushed_idx); action is "up"/"down"/None. Pure, no xbmc calls."""
    if idx is None:
        return None, last_idx, None
    if not is_pad:
        return None, idx, None
    if pushed_idx == idx:
        return None, idx, pushed_idx
    if last_idx is not None and idx < last_idx:
        return "up", idx, idx
    return "down", idx, idx


# ------------------------------------------------------- Kodi settings
def kodi_screensaver_mode():
    """Kodi's configured screensaver addon id ("" = no screensaver/off)."""
    try:
        resp = xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "Settings.GetSettingValue",
            "params": {"setting": "screensaver.mode"}}))
        val = json.loads(resp).get("result", {}).get("value", "")
        return val if isinstance(val, str) else ""
    except Exception:
        return ""


# ------------------------------------------------------- HEIC capability
#
# Which platforms can DECODE HEIC/HEIF? Kodi 22's native decode (FFmpeg 8) is
# not counted (cannot reassemble tiled/grid HEIC); cached per process lifetime.

_HEIC_CAPABLE = None


def _exe(path):
    try:
        return bool(path and os.path.isfile(path) and os.access(path, os.X_OK))
    except Exception:
        return False


def _which(name):
    try:
        import shutil
        return shutil.which(name)
    except Exception:
        return None


def _ffmpeg_heif():
    """True when an ffmpeg binary reports heif decoding support."""
    exe = _which("ffmpeg")
    if not exe:
        for cand in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if _exe(cand):
                exe = cand
                break
    if not exe:
        return False
    try:
        import subprocess
        r = subprocess.run([exe, "-hide_banner", "-decoders"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=10)
        return b"heif" in (r.stdout or b"")
    except Exception:
        return False


def heic_capable():
    """True when a HEIC/HEIF decoder we can rely on is present. Kodi 22's native decode is deliberately NOT counted (it cannot reassemble a tiled/grid HEIC); without a real decoder HEIC files are skipped rather than shown wrong."""
    global _HEIC_CAPABLE
    if _HEIC_CAPABLE is not None:
        return _HEIC_CAPABLE
    cap = bool(_which("sips")) or _exe("/usr/bin/sips")
    if not cap:
        try:
            from PIL import features
            cap = bool(features.check("libheif"))
        except Exception:
            cap = False
    if not cap:
        cap = _ffmpeg_heif()
    _HEIC_CAPABLE = cap
    return cap


# --- Install/update issue report (one-shot modal in boot.py) ----------------
# Non-fatal install/update problems are recorded here as STRUCTURED dicts and
# shown ONCE as our notice modal after boot (boot.py localizes the text).
ISSUE_FILE = "install-errors.json"


def record_issue(kind, **fields):
    """Append a structured issue (deduped, capped) to the install report."""
    try:
        path = state_file(ISSUE_FILE)
        data = read_json(path, None) or {}
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        entry = {"kind": str(kind)}
        entry.update(fields)
        if entry not in items and len(items) < 12:
            items.append(entry)
            write_json(path, {"items": items})
    except Exception:
        pass


def drop_issues(*kinds):
    """Remove report entries of the given kinds (e.g. a copy failure a later retry healed)."""
    try:
        path = state_file(ISSUE_FILE)
        data = read_json(path, None) or {}
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return
        kept = [i for i in items if not (isinstance(i, dict) and i.get("kind") in kinds)]
        if len(kept) == len(items):
            return
        if kept:
            write_json(path, {"items": kept})
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    except Exception:
        pass


def take_issues():
    """Return + clear the recorded issues (list of dicts)."""
    try:
        path = state_file(ISSUE_FILE)
        data = read_json(path, None) or {}
        items = data.get("items") if isinstance(data, dict) else []
        try:
            os.remove(path)
        except OSError:
            pass
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    except Exception:
        pass
    return []

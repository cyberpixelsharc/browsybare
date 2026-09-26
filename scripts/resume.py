"""Playback resume points (video + audio) in userdata/skindev/resume.json.

Own store: our playback builds its own folder playlist, so Kodi's native resume
prompt never fires, and the video DB is version/encoding fragile. Keys are a
SHA-256 of the credential-free path, so the file reveals no file/folder names
(lookup only -- nothing needs the reverse)."""
import hashlib
import time

from common import state_file, read_json, write_json, cache_key

FILE = "resume.json"
MIN_RESUME = 20.0   # positions below this are not worth a prompt
END_MARGIN = 15.0   # within this of the end counts as watched
MAX_AGE_DAYS = 49   # only resume points from the last 7 weeks count
MAX_ENTRIES = 300


def _key(path):
    raw = cache_key(path or "")
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def _is_key(s):
    return isinstance(s, str) and len(s) == 64 \
        and all(c in "0123456789abcdef" for c in s)


def _load():
    d = read_json(state_file(FILE), {})
    if not isinstance(d, dict):
        return {}
    changed = False
    for k in [k for k in d if not _is_key(k)]:  # migrate pre-hash raw-path keys
        d[_key(k)] = d.pop(k)
        changed = True
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    for k in [k for k, v in d.items()
              if not isinstance(v, dict) or v.get("at", 0) < cutoff]:
        d.pop(k, None)
        changed = True
    if changed:
        write_json(state_file(FILE), d)
    return d


def _write(d):
    if len(d) > MAX_ENTRIES:
        old = sorted(d.items(), key=lambda kv: kv[1].get("at", 0))
        for k, _ in old[:len(d) - MAX_ENTRIES]:
            d.pop(k, None)
    write_json(state_file(FILE), d)


def get(path):
    """Saved {'t','total','at'} for a path, or None."""
    e = _load().get(_key(path))
    if isinstance(e, dict) and e.get("t"):
        return e
    return None


def store(path, t, total):
    """Save the position, or drop the entry when watched (near the end)."""
    if not path or not total or total <= 0:
        return
    d = _load()
    k = _key(path)
    if t and 0 < t < total - END_MARGIN:
        d[k] = {"t": round(float(t), 1),
                "total": round(float(total), 1),
                "at": int(time.time())}
    else:
        d.pop(k, None)
    _write(d)


def clear(path):
    d = _load()
    if _key(path) in d:
        d.pop(_key(path), None)
        _write(d)


def promptable(entry):
    """True when the entry is worth a prompt."""
    try:
        return bool(entry) and entry.get("t", 0) >= MIN_RESUME \
            and entry.get("total", 0) > entry.get("t", 0) + END_MARGIN
    except Exception:
        return False


def fmt(seconds):
    """mm:ss or h:mm:ss."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, sec) if h else "%02d:%02d" % (m, sec)

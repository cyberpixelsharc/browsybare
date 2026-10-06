#!/usr/bin/env python3
"""On-demand source self-test (diagnostic).

Exercises a network source end to end -- create a folder, write a file, list it,
check its size, read it back, create a subfolder, copy, rename, move and delete
-- and logs a PASS/FAIL summary. Everything happens inside a temporary
"_bp-selftest" folder that is cleaned up again.

Run it against a stored source (by label or 1-based slot) or a raw URL:

  dev/source-selftest.sh "<label|slot|url>"     # via JSON-RPC
  RunScript(special://skin/scripts/selftest.py,"<label|slot|url>")

Log lines: `[skin] selftest: ...` (filter the Kodi log).
"""
import os
import sys
import time

import xbmcvfs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import log as _clog, redact
import sources
import fileops

TESTDIR = "_bp-selftest"


def _log(msg):
    _clog("selftest: " + msg)


def _resolve(target):
    """(url, label) for the target: a raw URL, or a stored network source by
    1-based slot or label."""
    t = (target or "").strip()
    if "://" in t:
        return t, "url"
    for i, e in enumerate(sources.netsrc_load()):
        if e.get("label") == t or str(i + 1) == t:
            return e.get("path") or "", e.get("label") or t
    return "", t


def _names(folder):
    """Child names of a network folder ('' list on failure)."""
    if sources.is_ftp(folder):
        r3 = sources.ftp_listdir(folder)
        return [] if r3[0] is None else list(r3[0]) + list(r3[1])
    try:
        res = xbmcvfs.listdir(sources.vfs_dir(folder))
        if isinstance(res, tuple) and len(res) == 2 and res[0] is not False:
            return list(res[0] or []) + list(res[1] or [])
    except Exception:
        pass
    return []


def _mkdir(parent, url):
    """Create one folder, then verify it by listing the parent (Kodi's VFS
    mkdir return value is unreliable for sftp)."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if sources.is_ftp(url):
        sources.ftp_mkdir(url)
    else:
        try:
            xbmcvfs.mkdir(url)
        except Exception:
            pass
    return name in _names(parent)


def _rename(src, dst):
    if sources.is_ftp(src):
        return bool(sources.ftp_rename(src, dst))
    try:
        return bool(xbmcvfs.rename(src, dst))
    except Exception:
        return False


def _read(path, limit=4096):
    try:
        f = xbmcvfs.File(path)
        data = f.readBytes(limit)
        f.close()
        return bytes(data) if data else b""
    except Exception:
        return None


def run(target):
    url, label = _resolve(target)
    _log("start %s (%s)" % (redact(url), label))
    if not url or "://" not in url:
        _log("FAIL: target not found: %r" % (target,))
        return False
    base = url.rstrip("/")
    root = base + "/" + TESTDIR
    results = []

    def rec(name, ok, extra=""):
        ok = bool(ok)
        results.append(ok)
        _log("%s %s%s" % ("OK  " if ok else "FAIL", name, " (%s)" % extra if extra else ""))

    try:
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), "bp-selftest-%s.bin" % os.urandom(4).hex())
        payload = ("BROWSYBARE-SELFTEST-%d" % time.time()).encode()
        with open(tmp, "wb") as f:
            f.write(payload)
    except Exception as e:
        _log("FAIL: local temp file: %s" % e)
        return False
    try:
        fileops._vfs_delete(root)   # clean up a leftover
        rec("mkdir test folder", _mkdir(base, root))
        rec("write file (copy local -> source)",
            fileops._copy_leaf(tmp, root + "/a.txt", {"total": 1, "done": 0}))
        rec("list shows a.txt", "a.txt" in _names(root), ", ".join(_names(root)[:6]))
        size = fileops._vfs_size(root + "/a.txt")
        rec("size matches", size == len(payload), "%s vs %s" % (size, len(payload)))
        data = _read(root + "/a.txt")
        rec("read back matches", data == payload,
            repr(bytes(data))[:40] if data is not None else "unreadable")
        rec("mkdir subfolder", _mkdir(root, root + "/sub"))
        rec("copy a.txt -> b.txt",
            fileops._copy_leaf(root + "/a.txt", root + "/b.txt", {"total": 1, "done": 0})
            and "b.txt" in _names(root))
        rec("rename b.txt -> c.txt",
            _rename(root + "/b.txt", root + "/c.txt")
            and "c.txt" in _names(root) and "b.txt" not in _names(root))
        rec("move c.txt -> sub/",
            _rename(root + "/c.txt", root + "/sub/c.txt")
            and "c.txt" in _names(root + "/sub") and "c.txt" not in _names(root))
        rec("delete test folder",
            fileops._vfs_delete(root) and TESTDIR not in _names(base))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    ok = all(results)
    _log("RESULT %s: %d/%d %s" % (label, sum(1 for x in results if x), len(results),
                                  "PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "")

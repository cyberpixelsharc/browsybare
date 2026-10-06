#!/usr/bin/env python3
"""On-demand source self-test (diagnostic).

Exercises a network source end to end -- create a folder, write a file, list it,
check its size, read it back, create a subfolder, copy, rename, move and delete
-- and logs a PASS/FAIL summary. A second pass repeats the round-trip with
umlauts and special characters in the names (stressing the transport's name
encoding: WebDAV percent-encodes child names, the other transports take them
literally). Everything happens inside a temporary "_bp-selftest" folder that is
cleaned up again.

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

# Special characters / umlauts for the second pass: stress the transport's name
# encoding end to end. Illegal-on-Windows characters (":*?<>|), ";" and "?" are
# avoided on purpose -- Kodi's own FTP read path truncates a name there (a known
# VFS limitation), which is not what this test is about.
SPECIAL_DIR = "Ü-Ordner & Prüfung (äöüß)"
SPECIAL_FILE = "Über & Prüf 100% äöüß.txt"
SPECIAL_COPY = "Kopie äöü & + (äöüß).txt"


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


def _child(parent, name):
    """Child URL in the parent transport's name form (WebDAV encodes it)."""
    return parent.rstrip("/") + "/" + sources.net_child_name(name, parent)


def _has(folder, name):
    """Does `folder` list a child with the logical name `name`? A WebDAV listing
    may carry the percent-encoded form, so also compare decoded."""
    ns = _names(folder)
    if name in ns:
        return True
    if sources.is_dav(folder):
        if sources.net_child_name(name, folder) in ns:
            return True
        return name in [sources.url_unquote(n) for n in ns]
    return False


def _listed_url(folder, logical):
    """The verbatim child URL a listing reports for `logical`. A WebDAV listing
    keeps the server href form -- exactly the form the app hands to Kodi for a
    file read/playback, so it is the form the test must read with too."""
    for n in _names(folder):
        if n == logical or sources.url_unquote(n) == logical:
            return folder.rstrip("/") + "/" + n
    return _child(folder, logical)


def _mkdir(parent, url, name=None):
    """Create one folder, then verify it by listing the parent (Kodi's VFS
    mkdir return value is unreliable for sftp)."""
    if name is None:
        name = url.rstrip("/").rsplit("/", 1)[-1]
    if sources.is_ftp(url):
        sources.ftp_mkdir(url)
    else:
        try:
            xbmcvfs.mkdir(url)
        except Exception:
            pass
    return _has(parent, name)


def _rename(src, dst):
    if sources.is_ftp(src):
        return bool(sources.ftp_rename(src, dst))
    try:
        return bool(xbmcvfs.rename(src, dst))
    except Exception:
        return False


def _read(path, limit=4096):
    """Read a network file via the VFS; b'' on an empty read, None when
    unreadable."""
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
    if url.lower().startswith("sftp://") and not sources.host_reachable(url):
        # vfs.sftp dereferences a null session on an unreachable host and
        # SEGFAULTS the whole process (verified: deleteFile after the server
        # stopped). Never touch the VFS then -- bail out on the TCP probe.
        _log("FAIL: sftp host unreachable (Kodi's vfs.sftp would crash)")
        return False
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
        rec("list shows a.txt", _has(root, "a.txt"), ", ".join(_names(root)[:6]))
        size = fileops._vfs_size(root + "/a.txt")
        rec("size matches", size == len(payload), "%s vs %s" % (size, len(payload)))
        data = _read(root + "/a.txt")
        rec("read back matches", data == payload,
            repr(bytes(data))[:40] if data is not None else "unreadable")
        rec("mkdir subfolder", _mkdir(root, root + "/sub"))
        rec("copy a.txt -> b.txt",
            fileops._copy_leaf(root + "/a.txt", root + "/b.txt", {"total": 1, "done": 0})
            and _has(root, "b.txt"))
        rec("rename b.txt -> c.txt",
            _rename(root + "/b.txt", root + "/c.txt")
            and _has(root, "c.txt") and not _has(root, "b.txt"))
        rec("move c.txt -> sub/",
            _rename(root + "/c.txt", root + "/sub/c.txt")
            and _has(root + "/sub", "c.txt") and not _has(root, "c.txt"))
        # Second pass: umlauts and special characters in the names.
        sdir = _child(root, SPECIAL_DIR)
        sfile = _child(sdir, SPECIAL_FILE)
        sfile2 = _child(sdir, SPECIAL_COPY)
        rec("mkdir special-char folder", _mkdir(root, sdir, SPECIAL_DIR))
        rec("write special-char file",
            fileops._copy_leaf(tmp, sfile, {"total": 1, "done": 0}))
        rec("list shows special-char file", _has(sdir, SPECIAL_FILE),
            ", ".join(_names(sdir)[:6]))
        rec("special-char size matches",
            fileops._vfs_size(sfile) == len(payload))
        # Read back / copy with the whole path in the LISTING href form -- the
        # form the app browses/plays/copies a WebDAV child with. (A
        # quote()-encoded DAV folder path, as the writes use, reads back EMPTY:
        # Kodi's DAV open wants the server href form, verified live.)
        sdir_href = _listed_url(root, SPECIAL_DIR)
        sfile_href = _listed_url(sdir_href, SPECIAL_FILE)
        sdata = _read(sfile_href)
        rec("special-char read back matches", sdata == payload,
            repr(bytes(sdata))[:40] if sdata is not None else "unreadable")
        rec("copy special-char file",
            fileops._copy_leaf(sfile_href, sfile2, {"total": 1, "done": 0})
            and _has(sdir, SPECIAL_COPY))
        rec("delete test folder",
            fileops._vfs_delete(root) and not _has(base, TESTDIR))
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

"""Home boot sequence: all Home initialization in ONE Python interpreter
(concurrent interpreter startup segfaults Kodi). boot.lock serializes overlapping boots; daemons start staggered and every step is guarded."""
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xbmc
import xbmcgui
import xbmcvfs

from common import (log, state_dir, read_json, write_json, skin_root,
                    record_issue, take_issues, kodi_screensaver_mode, L, redact)
from sync import sync, accents, skin_version
import main
import sources
import volume


def _ver_tuple(v):
    import re
    try:
        return tuple(int(t) for t in re.findall(r"\d+", v or ""))
    except Exception:
        return ()


def _fmt(sid, *args):
    """Localized string with %-args, never raising (a missing string would make `%` throw)."""
    try:
        return L(sid) % args
    except Exception:
        try:
            return "%s %s" % (L(sid), " / ".join(str(a) for a in args))
        except Exception:
            return str(sid)


def _wait_home(max_s=8.0):
    """Wait until the Home window is loaded; False if it never came up (a modal shown during ReloadSkin is destroyed with the reload)."""
    end = time.time() + max_s
    while True:
        try:
            if xbmc.getCondVisibility("Window.IsActive(10000)"):
                return True
        except Exception:
            return True
        if time.time() >= end:
            return False
        time.sleep(0.25)


def _show_notice(title, body, focus=None):
    """Show our notice modal (bp.notice, Layout.xml): `body` is a string or a list of rows (bp.notice.1-10); `focus` is the control id to focus (default OK, 862)."""
    try:
        win = xbmcgui.Window(10000)
        # bp.notice.head survives a Home re-activation (not cleared by onload).
        win.setProperty("bp.notice.head", title or "")
        lines = [body] if isinstance(body, str) else list(body or [])
        for i in range(1, 11):
            win.setProperty("bp.notice.%d" % i,
                            lines[i - 1] if i <= len(lines) else "")
        win.setProperty("bp.notice.focus", focus or "862")
        xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,notice)")
    except Exception as e:
        log("boot: notice open failed: %s" % e)


def _consume_flag(flag_file):
    """Atomically consume the one-shot reinstall flag; True when we own it (whichever post-reload entry point removes it first wins)."""
    try:
        os.remove(flag_file)
        return True
    except OSError:
        return False


def check_version_warning():
    """Record a downgrade / same-version reinstall warning as an install issue (shown by check_install_errors). State lives in userdata/skindev and survives the updater's wipe; a same-version reinstall warns only with the one-shot flag set by a wipe-triggered sync."""
    try:
        cur = skin_version()
        if not cur or cur == "?":
            return
        last_file = os.path.join(state_dir(), "last-version.json")
        flag_file = os.path.join(state_dir(), "version-reinstall.json")
        last = read_json(last_file, None) or {}
        stored = last.get("version")
        flag = read_json(flag_file, None) or {}
        pending = bool(flag.get("pending"))
        down = bool(stored) and _ver_tuple(cur) < _ver_tuple(stored)
        same = bool(stored) and cur == stored and pending
        if not (down or same):
            # Consume a wipe flag even without a warning, else the next boot
            # raises a spurious "reinstall" for a normal update.
            if pending:
                _consume_flag(flag_file)
            if stored != cur:
                write_json(last_file, {"version": cur})
            return
        # A pending flag marks a wipe install; the atomic consume decides who
        # records it (both entry points can get here; record_issue dedupes).
        own = _consume_flag(flag_file) if pending else True
        if down and own:
            record_issue("version_downgrade", new=cur, old=stored)
            log("boot: downgrade warning recorded (%s over %s)" % (cur, stored))
        elif same and own:
            record_issue("version_reinstall", ver=cur)
            log("boot: reinstall warning recorded (same version %s)" % cur)
        if stored != cur:
            write_json(last_file, {"version": cur})
    except Exception as e:
        log("boot: version watch failed: %s" % e)


def _addon_id():
    """The skin's addon id from addon.xml (== its addon_data folder name)."""
    try:
        with open(os.path.join(skin_root(), "addon.xml"), "r", encoding="utf-8") as f:
            head = f.read(2000)
        m = re.search(r'<addon[^>]*?\sid="([^"]+)"', head)
        return m.group(1) if m else ""
    except Exception:
        return ""


def ensure_addon_data():
    """Ensure userdata/addon_data/<id>/ exists and is writable; a corrupt directory silently blocks Kodi's settings save, so detect with a write probe and self-heal by recreating it."""
    aid = _addon_id()
    if not aid:
        return
    try:
        base = xbmcvfs.translatePath("special://profile/addon_data")
    except Exception:
        return
    path = os.path.join(base, aid)
    probe = os.path.join(path, ".writeprobe")

    def write_ok():
        try:
            os.makedirs(path, exist_ok=True)
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            return True
        except Exception as e:
            write_ok.err = e
            return False
    write_ok.err = None

    if write_ok():
        return
    log("boot: addon_data not writable (%s), self-healing %s" % (write_ok.err, redact(path)))
    # A corrupt directory cannot be repaired in place: empty + remove, then
    # recreate (listdir may itself fail -- still attempt the rmdir).
    try:
        for entry in os.listdir(path):
            p = os.path.join(path, entry)
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass
    for _ in range(2):
        try:
            os.rmdir(path)
        except OSError:
            pass
        if write_ok():
            log("boot: addon_data self-healed (%s)" % redact(path))
            record_issue("addon_data_healed")
            return
    log("boot: addon_data self-heal FAILED (%s)" % write_ok.err)
    record_issue("addon_data_failed")


def _fmt_issue(it):
    """Localized one-line text for a recorded install/update issue."""
    kind = it.get("kind")
    try:
        if kind == "copy_failed":
            return L(31479)
        if kind == "addon_data_healed":
            return L(31480)
        if kind == "addon_data_failed":
            return L(31481)
        if kind == "sync_failed":
            return L(31482)
        if kind == "version_downgrade":
            return _fmt(31476, it.get("new") or "", it.get("old") or "")
        if kind == "version_reinstall":
            v = it.get("ver") or ""
            return _fmt(31477, v, v)
    except Exception:
        pass
    return str(kind)


def check_install_errors():
    """One-shot notice modal for issues recorded during the last install/update (base-copy failures, addon_data self-heal); shown only when something was recorded. Runs at the same safe points as check_version_warning."""
    try:
        issues = take_issues()
        if not issues:
            return
        if not _wait_home():
            # Home not up yet: keep the report for the next boot.
            for it in issues:
                record_issue(it.get("kind", "?"),
                             **{k: v for k, v in it.items() if k != "kind"})
            return
        # Never cover the skin-keep confirm dialog (10100; "No" reverts the
        # skin): if it is up, keep the report for the next boot.
        try:
            if xbmc.getCondVisibility("Window.IsActive(10100)"):
                for it in issues:
                    record_issue(it.get("kind", "?"),
                                 **{k: v for k, v in it.items() if k != "kind"})
                log("boot: install report deferred (skin-keep dialog open)")
                return
        except Exception:
            pass
        lines = [l for l in (_fmt_issue(it) for it in issues) if l][:10]
        _show_notice(L(31478), lines)
        log("boot: install report shown (%d issue(s))" % len(issues))
    except Exception as e:
        log("boot: install report failed: %s" % e)


def step(name, fn):
    try:
        fn()
        log("boot: %s done" % name)
    except Exception as e:
        log("boot: %s failed: %s" % (name, e))


def start_daemon(name):
    try:
        xbmc.executebuiltin("RunScript(special://skin/scripts/%s)" % name)
        log("boot: %s started" % name)
    except Exception as e:
        log("boot: %s start failed: %s" % (name, e))


LOCK_STALE = 180.0  # a base copy takes seconds; older = dead owner


def _lock_path():
    try:
        return os.path.join(state_dir(), "boot.lock")
    except Exception:
        return ""


def acquire_boot_lock():
    """Single-instance guard for the whole boot (sync copy + reload): two
    concurrent boots re-copy the base and stack ReloadSkin calls, leaving Home half-loaded (black screen). The latecomer exits; a stale lock is stolen."""
    p = _lock_path()
    if not p:
        return True
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(p)
            except OSError:
                return True
            # A DEAD owner must not block the base sync for up to LOCK_STALE: a
            # boot killed while Kodi rewrites the addon dir (zip install) would
            # otherwise leave the next boot (the post-install reload) without a
            # base layer -- a black Home until the lock ages out.
            try:
                with open(p, "r", encoding="utf-8") as f:
                    owner = int((f.read().strip() or "0"))
            except Exception:
                owner = 0
            if owner > 0:
                try:
                    os.kill(owner, 0)
                except ProcessLookupError:
                    log("boot: lock owner %d is gone, stealing" % owner)
                    age = LOCK_STALE + 1.0
                except Exception:
                    pass
            if age <= LOCK_STALE:
                return False
            # Steal ATOMICALLY via rename: only one of two racing boots wins
            # (the next loop iteration recreates the lock with O_EXCL).
            side = "%s.stale.%d" % (p, os.getpid())
            try:
                os.rename(p, side)
                log("boot: stole stale lock (age %.0fs)" % age)
                try:
                    os.remove(side)
                except OSError:
                    pass
            except OSError:
                return False
        except OSError:
            return True
    return False


def release_boot_lock():
    try:
        p = _lock_path()
        if not p:
            return
        with open(p, "r", encoding="utf-8") as f:
            owner = f.read().strip()
        # Only remove OUR lock: a newer boot may have stolen a stale lock.
        if owner == str(os.getpid()):
            os.remove(p)
    except OSError:
        pass


def migrate_drivevis():
    """One-time migration hide.drive.N (opt-out) -> show.drive.N (opt-in): hidden stays hidden, everything else becomes explicitly shown. Guarded by drivevis.migrated."""
    try:
        if xbmc.getCondVisibility("Skin.HasSetting(drivevis.migrated)"):
            return
    except Exception:
        return
    for i in range(1, 7):
        try:
            show = xbmc.getCondVisibility("Skin.HasSetting(show.drive.%d)" % i)
            hide = xbmc.getCondVisibility("Skin.HasSetting(hide.drive.%d)" % i)
        except Exception:
            continue
        try:
            if not show and not hide:
                xbmc.executebuiltin("Skin.SetBool(show.drive.%d)" % i)
            if hide:
                xbmc.executebuiltin("Skin.Reset(hide.drive.%d)" % i)
        except Exception:
            pass
    try:
        xbmc.executebuiltin("Skin.SetBool(drivevis.migrated)")
        log("boot: drivevis migrated hide.drive.N -> show.drive.N")
    except Exception:
        pass


def seed_dimscreen():
    """One-time: mirror Kodi's screensaver setting into the skin option `dim.screen` (screensaver configured -> ON, none -> OFF). Guarded; a later user choice is never touched."""
    try:
        if xbmc.getCondVisibility("Skin.HasSetting(dimscreen.seeded)"):
            return
    except Exception:
        return
    on = bool(kodi_screensaver_mode().strip())
    try:
        if on:
            xbmc.executebuiltin("Skin.SetBool(dim.screen)")
        xbmc.executebuiltin("Skin.SetBool(dimscreen.seeded)")
        log("boot: dim.screen default seeded %s (Kodi screensaver)" % on)
    except Exception:
        pass


def migrate_foldersfirst():
    """One-time migration folders.first (opt-in) -> folders.last (opt-out, default ON); a set flag simply clears. Guarded."""
    try:
        if xbmc.getCondVisibility("Skin.HasSetting(foldersfirst.migrated)"):
            return
    except Exception:
        return
    try:
        if xbmc.getCondVisibility("Skin.HasSetting(folders.first)"):
            xbmc.executebuiltin("Skin.Reset(folders.first)")
    except Exception:
        pass
    try:
        xbmc.executebuiltin("Skin.SetBool(foldersfirst.migrated)")
        log("boot: foldersfirst migrated to opt-out folders.last")
    except Exception:
        pass


def migrate_drive_identity():
    """One-time migration positional show.drive.N -> identity keys: a set positional flag maps onto the drive CURRENTLY at that slot, then clears. Guarded by drivevis.identity."""
    try:
        if xbmc.getCondVisibility("Skin.HasSetting(drivevis.identity)"):
            return
    except Exception:
        return
    try:
        regular = [d for d in sources.all_drives()
                   if d.get("type") not in ("dirsource", "home") and not d.get("system")]
    except Exception:
        regular = []
    for i in range(1, 7):
        try:
            if not xbmc.getCondVisibility("Skin.HasSetting(show.drive.%d)" % i):
                continue
            if i - 1 < len(regular):
                xbmc.executebuiltin("Skin.SetBool(show.drive.%s)" % sources.drive_key(regular[i - 1]))
            xbmc.executebuiltin("Skin.Reset(show.drive.%d)" % i)
        except Exception:
            pass
    try:
        xbmc.executebuiltin("Skin.SetBool(drivevis.identity)")
        log("boot: drivevis migrated positional show.drive.N -> identity keys")
    except Exception:
        pass


def migrate_drive_optout():
    """Retired show.drive.<identity> (opt-in) -> visible-by-default cleanup: only clears the retired opt-in key and auto-hides NOTHING (the toggle now sets hidden.drive.<identity>). Guarded by drivevis.optout."""
    try:
        if xbmc.getCondVisibility("Skin.HasSetting(drivevis.optout)"):
            return
    except Exception:
        return
    try:
        regular = [d for d in sources.all_drives()
                   if d.get("type") not in ("dirsource", "home") and not d.get("system")]
    except Exception:
        regular = []
    for d in regular:
        try:
            key = sources.drive_key(d)
            xbmc.executebuiltin("Skin.Reset(show.drive.%s)" % key)
        except Exception:
            pass
    try:
        xbmc.executebuiltin("Skin.SetBool(drivevis.optout)")
        log("boot: drivevis.optout cleanup (visible-by-default, no auto-hide)")
    except Exception:
        pass


def focus_default():
    """Park focus on the drive chip (30) once the file manager is visible: the ready-gate hides the layout at Home load, so defaultcontrol never lands. Never yanks focus from a user who already navigated."""
    for _ in range(8):
        try:
            if xbmc.getCondVisibility("String.IsEqual(Window(10000).Property(bp.ready),1)"):
                break
        except Exception:
            pass
        time.sleep(0.2)
    # Returning from the settings DIALOG (Custom1150.xml): restore its focus hint.
    try:
        _ret = xbmcgui.Window(10000).getProperty("bp.return.focus")
        if _ret and _ret.isdigit() and \
                xbmcgui.Window(10000).getProperty("bp.pick.active") != "1":
            xbmcgui.Window(10000).clearProperty("bp.return.focus")
            time.sleep(0.25)
            xbmc.executebuiltin("SetFocus(%s)" % _ret)
            log("boot: return focus -> %s" % _ret)
            return
    except Exception:
        pass
    # Retry a few ticks: the gate needs a frame to paint and SetFocus on a
    # not-yet-visible control is a no-op on some builds.
    guard = ("[Control.HasFocus(30)|Control.HasFocus(33)|Control.HasFocus(32)|"
             "Control.HasFocus(74)|"
             "String.IsEqual(Window(10000).Property(bp.settings),open)|"
             "String.IsEqual(Window(10000).Property(bp.menu),open)|"
             "String.IsEqual(Window(10000).Property(bp.drives),open)|"
             "String.IsEqual(Window(10000).Property(bp.about),open)|"
             "String.IsEqual(Window(10000).Property(bp.power),open)|"
             "String.IsEqual(Window(10000).Property(bp.ctx),open)|"
             "String.IsEqual(Window(10000).Property(bp.rowmenu),open)|"
             "String.IsEqual(Window(10000).Property(bp.del),open)|"
             "String.IsEqual(Window(10000).Property(bp.kb),open)|"
             "String.IsEqual(Window(10000).Property(bp.notice),open)|"
             "String.IsEqual(Window(10000).Property(bp.pick.active),1)]")
    for _ in range(6):
        time.sleep(0.25)
        try:
            if xbmc.getCondVisibility(guard):
                return  # focus already somewhere sane (or an overlay is open)
        except Exception:
            pass
        try:
            xbmc.executebuiltin("SetFocus(30)")
        except Exception as e:
            log("boot: default focus failed: %s" % e)
            return
        try:
            if xbmc.getCondVisibility("Control.HasFocus(30)"):
                log("boot: default focus -> chip 30")
                return
        except Exception:
            pass
    log("boot: default focus not verified")


def reload_when_ready(force=False):
    """One-time skin reload after a fresh base sync; never reload under the open keep-dialog (10100) and never twice within RELOAD_COOLDOWN (stacked reloads are the black-screen mechanism). Reloads only after 2s idle (hard cap 45s); force=True bypasses the cooldown."""
    RELOAD_COOLDOWN = 90.0
    STACK_GUARD = 8.0
    win = xbmcgui.Window(10000)
    fired = False
    try:
        try:
            last = float(win.getProperty("bp.reload.t") or 0)
        except (TypeError, ValueError):
            last = 0
        now = time.time()
        if last and now - last < RELOAD_COOLDOWN:
            if not force:
                log("boot: reload skipped (cooldown, last %.0fs ago)" % (now - last))
                return
            # Forced (menu changed): never stack onto a reload still settling.
            if now - last < STACK_GUARD:
                if xbmc.Monitor().waitForAbort(STACK_GUARD - (now - last)):
                    return
        monitor = xbmc.Monitor()
        # Minimum veil time: on fast disks the copy finishes in milliseconds
        # and the veil would flash subliminally.
        try:
            t0 = float(win.getProperty("bp.sync.t0") or 0)
        except (TypeError, ValueError):
            t0 = 0
        if t0:
            remaining = 1.0 - (time.time() - t0)
            if remaining > 0 and monitor.waitForAbort(remaining):
                return
        # One-shot .devswitch marker (never shipped) shortens the no-dialog
        # wait to 2s; a really open dialog still waits via the seen logic.
        nodlg = 8
        try:
            from common import state_dir as _sd
            marker = os.path.join(_sd(), ".devswitch")
            if os.path.isfile(marker) and time.time() - os.path.getmtime(marker) < 120:
                nodlg = 2
                log("boot: dev-switch marker, short dialog wait")
            try:
                os.remove(marker)
            except OSError:
                pass
        except Exception:
            pass
        seen = False
        for elapsed in range(45):
            if monitor.abortRequested():
                return
            try:
                is_open = xbmc.getCondVisibility("Window.IsActive(10100)")
            except Exception:
                is_open = False
            if is_open:
                seen = True
            elif seen or elapsed >= nodlg:
                try:
                    idle = xbmc.getCondVisibility("System.IdleTime(2)")
                except Exception:
                    idle = True
                if idle:
                    break
                if elapsed >= 44:
                    log("boot: reload forced after cap (user active)")
                    break
            monitor.waitForAbort(1.0)
        if monitor.abortRequested():
            return
        try:
            win.setProperty("bp.reload.t", str(time.time()))
            xbmc.executebuiltin("ReloadSkin()")
            fired = True
            log("boot: fresh base ready, skin reloaded")
        except Exception as e:
            log("boot: reload failed: %s" % e)
    finally:
        veil = False
        try:
            veil = win.getProperty("bp.sync.active") == "1"
        except Exception:
            pass
        if fired and veil:
            # Base veil up + reload in flight: KEEP the veil (revealing now
            # flashes the old Home); the reloaded Home reveals itself.
            try:
                xbmc.executebuiltin(
                    "AlarmClock(bp_ready,RunScript(special://skin/scripts/boot.py,ready),00:02,silent)")
                log("boot: reload in flight, veil kept + ready fallback armed")
            except Exception as e:
                log("boot: ready fallback failed: %s" % e)
                try:
                    win.clearProperty("bp.sync.active")
                    win.setProperty("bp.ready", "1")
                except Exception:
                    pass
                focus_default()
        else:
            # No base veil (menu-only reload) or no reload: reveal the UI now.
            try:
                win.clearProperty("bp.sync.active")
                win.clearProperty("bp.sync.t0")
                win.clearProperty("bp.sync.phase")
                win.setProperty("bp.ready", "1")
            except Exception:
                pass
            focus_default()


def run():
    if not acquire_boot_lock():
        log("boot: another boot in progress, exit")
        return
    try:
        step("addon_data", ensure_addon_data)
        step("drivevis", migrate_drivevis)
        step("drivevis.identity", migrate_drive_identity)
        step("drivevis.optout", migrate_drive_optout)
        step("foldersfirst", migrate_foldersfirst)
        step("dimscreen.seed", seed_dimscreen)
        try:
            did_copy = bool(sync())
        except Exception as e:
            did_copy = False
            log("boot: sync failed: %s" % e)
            record_issue("sync_failed")
        # The generated Beenden include is parsed before this onload; force a
        # reload when sync rewrote it, even without a base copy.
        try:
            _w = xbmcgui.Window(10000)
            menu_changed = _w.getProperty("bp.powermenu.changed") == "1"
            _w.clearProperty("bp.powermenu.changed")
        except Exception:
            menu_changed = False
        log("boot: sync done (did_copy=%s menu_changed=%s)" % (did_copy, menu_changed))
        step("drives", main.drives)
        step("blacklist", main.blacklist_open)
        step("dirsources", main.dirsrc_open)
        step("netsources", main.netsrc_open)
        step("remotes", main.remote_open)
        step("accents", accents)
        step("volume", volume.init)
        step("refreshdelay", main.refreshdelay)
        step("guisound", main.guisound)
        if did_copy or menu_changed:
            reload_when_ready(force=menu_changed)
        else:
            # No copy, no reload coming: the UI is final, show it (the copy
            # path sets ready in reload_when_ready's finally instead).
            try:
                xbmcgui.Window(10000).setProperty("bp.ready", "1")
            except Exception:
                pass
            focus_default()
        start_daemon("foldersize.py")
        try:
            xbmc.Monitor().waitForAbort(1.0)
        except Exception:
            pass
        start_daemon("home-daemon.py")
        if not (did_copy or menu_changed):
            # No reload coming: a pre-reload dialog would die with ReloadSkin,
            # so the version/install watch runs here (copy path: post-reload boot).
            check_version_warning()
            check_install_errors()
    finally:
        release_boot_lock()


def ready_only():
    """Fallback reveal after the post-sync skin reload: clear the veil, mark the UI ready, land default focus, and run the version/install checks. Invoked via AlarmClock in case the reloaded Home's boot is skipped by the boot lock."""
    win = xbmcgui.Window(10000)
    try:
        win.clearProperty("bp.sync.active")
        win.clearProperty("bp.sync.t0")
        win.clearProperty("bp.sync.phase")
        win.setProperty("bp.ready", "1")
    except Exception:
        pass
    focus_default()
    # Reliable post-reload point for the version/install watch (the reloaded
    # Home's boot is often blocked by the still-held boot.lock).
    check_version_warning()
    check_install_errors()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ready":
        ready_only()
    else:
        run()

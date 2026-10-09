#!/usr/bin/env python3
"""Single Home watchdog daemon: OSD/volume/PVR/padding/focus duties.

Kodi stops Python scripts serially and waits 5s per script at quit, so the
former separate daemons share one invoker. Single instance via the
bp.home-daemon.running takeover token (a newer token makes the old one exit).
"""
import json
import os
import re
import time
from urllib.parse import unquote

import xbmc
import xbmcgui
import xbmcvfs

from common import log, redact, padding_step

import fileops
import sources
import remotes
import resume
import sync
import volume

RUNNING = "bp.home-daemon.running"
TAKEOVER_GRACE = 3.0
VOLUME_TICKS = 40  # even-tick gate -> 40 = 2 s
VOLUME_SLIDER_IDS = (400, 410)
STOP_FOCUS_WINDOW = 3.0
OSD_ADVANCE_WINDOW = 7.0
# Settle window around a player start/stop: reading container/info labels then
# crashed Kodi 22 (SIGSEGV in CGUIInfoManager::GetMultiInfoLabel).
PLAYER_SETTLE = 4.0
# Rows to scroll past the followed track so it clears the header/footer overlay
# (Kodi scrolls the focused row only to the nearest edge).
FOLLOW_OFFSET = 8
# Controls that can still hold focus after the player UI hides on stop
# (fallback chip 30 + audio footer rows/sliders); duty 6 returns focus to the list.
STOP_FOCUS_IDS = (30,) + tuple(range(330, 359)) + VOLUME_SLIDER_IDS


def _is_playing():
    try:
        return bool(xbmc.getCondVisibility("Player.HasVideo") or xbmc.getCondVisibility("Player.HasAudio"))
    except Exception:
        return False


def _prefetch_metadata(path):
    """Warm the just-started file's metadata in the background so opening INFO
    is instant (a network scan otherwise blocks for seconds)."""

    def _work():
        try:
            import metadata
            m = metadata.load_cache(path)
            if m is None:
                m = metadata.read_info(path)
                metadata.store_cache(path, m)
            if isinstance(m, dict):
                metadata.covers(path)
        except Exception as e:
            log("home-daemon: metadata prefetch failed: %s" % e)

    try:
        import threading
        threading.Thread(target=_work, daemon=True).start()
    except Exception:
        pass


def _stop_focus_stuck():
    """True while focus sits on a control that just hid with the player UI
    after a stop."""
    try:
        for cid in STOP_FOCUS_IDS:
            if xbmc.getCondVisibility("Control.HasFocus(%d)" % cid):
                return True
    except Exception:
        pass
    return False


def get_volume():
    """Current System.Volume via JSON-RPC Application.GetProperties."""
    try:
        resp = xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "Application.GetProperties",
            "params": {"properties": ["volume", "muted"]}
        }))
        data = json.loads(resp)
        vol = data.get("result", {}).get("volume")
        if isinstance(vol, (int, float)):
            return int(vol)
    except Exception:
        pass
    try:
        s = xbmc.getInfoLabel("Player.Volume")
        if s:
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", s)
            if m:
                return int(float(m.group(1)))
    except Exception:
        pass
    return -1


def get_muted():
    """Kodi's real mute flag, or None. Player.Muted also reports true at volume 0,
    so the skin mirrors this into bp.vol.mute for the mute icons."""
    try:
        resp = xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "Application.GetProperties",
            "params": {"properties": ["muted"]}}))
        return json.loads(resp).get("result", {}).get("muted")
    except Exception:
        return None


def _slider_percent(sid):
    """Percent of the hidden volume input slider (label "NN%"), or -1."""
    try:
        s = xbmc.getInfoLabel("Control.GetLabel(%d)" % sid)
    except Exception:
        return -1
    if not s:
        return -1
    m = re.search(r"([0-9]+)", s)
    return int(m.group(1)) if m else -1


def photo_progress(due, iv, now):
    """Slideshow progress 0-100 toward the next image (elapsed share of the
    interval; bad/zero interval -> 0)."""
    try:
        iv = float(iv)
    except (TypeError, ValueError):
        return 0
    if not iv or iv <= 0:
        return 0
    try:
        left = float(due) - float(now)
    except (TypeError, ValueError):
        return 0
    pct = 100.0 * (1.0 - left / iv)
    return max(0, min(100, int(pct)))


def is_kb_open(win):
    try:
        return win.getProperty("bp.kb") == "open"
    except Exception:
        return False


SCAN_TAIL = 65536
_scan_logpath = None


def _scan_last_line(since):
    """Newest kodi.log HandleKey line past `since` as (key, btn, action), else
    None. Reads only the log tail; needs debug logging."""
    global _scan_logpath
    try:
        if _scan_logpath is None:
            _scan_logpath = os.path.join(xbmcvfs.translatePath("special://logpath"), "kodi.log")
        with open(_scan_logpath, "rb") as f:
            try:
                f.seek(-SCAN_TAIL, os.SEEK_END)
            except Exception:
                f.seek(0)
            chunk = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    best = None
    for line in chunk.splitlines():
        if "HandleKey" not in line or len(line) < 24:
            continue
        if line[:19] <= since:
            continue
        m = re.search(r"HandleKey.*?(\S+) \((0x[0-9a-fA-F]+)(?:, ([^)]+))?\) pressed", line)
        if not m:
            continue
        key, _, btn = m.groups()
        am = re.search(r"action is (.*)$", line)
        action = (am.group(1) if am else "").strip()
        best = (key, btn, action)
    return best


def scan_last_key_raw(since):
    """Raw KEYMAP key name of the newest HandleKey line (obc code for remote
    lines), else ''."""
    r = _scan_last_line(since)
    if not r:
        return ""
    key, btn, _ = r
    return btn if (btn or "").startswith("obc") else key


def is_pvr_dialog_visible():
    # While our keyboard (bp.kb) is open, any PVR/shutdown dialog is the
    # accidental hardware shortcut (Kodi 21 keymaps have no conditions) -> close it.
    try:
        for wid in ("12002", "10100", "12000", "12001", "11100"):
            if xbmc.getCondVisibility("Window.IsVisible(%s)" % wid):
                try:
                    heading = xbmc.getInfoLabel("Window(%s).Property(1)" % wid) or xbmc.getInfoLabel("Control.GetLabel(1)") or xbmc.getInfoLabel("Window.Property(1)")
                    if heading and ("PVR" in heading or "Ausschalten" in heading or "Verlassen" in heading):
                        return True
                    if wid in ("12002", "10100"):
                        for lbl in (xbmc.getInfoLabel("Control.GetLabel(1)"), xbmc.getInfoLabel("Control.GetLabel(2)"), xbmc.getInfoLabel("Window(%s).Property(1)" % wid)):
                            if lbl and ("Ausschalten" in lbl or "Verlassen" in lbl or "Neustart" in lbl):
                                return True
                        return True
                except Exception:
                    return True
        try:
            for label in (xbmc.getInfoLabel("Window.Property(1)"), xbmc.getInfoLabel("Control.GetLabel(1)"), xbmc.getInfoLabel("Control.GetLabel(2)")):
                if label and ("PVR" in label or "Ausschalten" in label or "Verlassen" in label):
                    return True
        except Exception:
            pass
        return False
    except Exception:
        return False


def overlay_open(win):
    """True while any Home overlay/picker/keyboard state is active."""
    try:
        for prop in ("bp.drives", "bp.menu", "bp.settings", "bp.ctx",
                     "bp.del", "bp.about", "bp.keys", "bp.kb", "bp.power", "bp.notice",
                     "bp.info"):
            if win.getProperty(prop) == "open":
                return True
        if win.getProperty("bp.pick.active") == "1":
            return True
        if win.getProperty("bp.sync.active") == "1":
            return True
    except Exception:
        return True
    return False


def _focus_list_on(path, footer_focus="", prev_idx=-1):
    """Focus/scroll list 33 onto `path` via the plugin-published basename list
    (bp.list.keys), clear of the header/footer overlay, then restore the previous
    focus so the footer stays usable. Returns the row index, or -1 when the file
    is not in the current folder. Centring the row is parked (Kodi has no
    scroll-to-centre builtin)."""
    keys = xbmcgui.Window(10000).getProperty("bp.list.keys")
    if not keys:
        return -1
    # Network URLs arrive percent-encoded from getPlayingFile; the key list is
    # already decoded -- unquote both so names with spaces still match.
    base = unquote(os.path.basename(path.rstrip("/")))
    rows = keys.split("\n")
    for i, k in enumerate(rows):
        if k and unquote(k) == base:
            # Kodi scrolls the focused row only to the nearest edge, where the
            # header/footer overlay hides it. Focus a row a few positions further
            # in the scroll direction (so the target clears the overlay), then the
            # row itself (already visible -> no scroll) so its pill sits on the
            # playing item. Direction from the previous follow index; near an end
            # jump to the last possible row (Kodi ignores an out-of-range focus).
            if i >= prev_idx:
                far = min(len(rows) - 1, i + FOLLOW_OFFSET)
            else:
                far = max(0, i - FOLLOW_OFFSET)
            prev = xbmc.getInfoLabel("System.CurrentControlId") or ""
            xbmc.executebuiltin("SetFocus(33,%d,absolute)" % far)
            time.sleep(0.1)
            xbmc.executebuiltin("SetFocus(33,%d,absolute)" % i)
            time.sleep(0.2)
            # System.CurrentControlId reads empty right at a track change; fall
            # back to the last footer control seen, else focus stays in the list.
            if prev.isdigit() and prev != "33":
                target = prev
            elif prev == "33":
                target = ""
            else:
                target = footer_focus
            if target.isdigit():
                xbmc.executebuiltin("SetFocus(%s)" % target)
            log("home-daemon: list focus -> %d" % i)
            return i
    log("home-daemon: list follow miss (%d keys)" % (keys.count("\n") + 1))
    return -1


def main():
    win = xbmcgui.Window(10000)
    now = time.time()
    try:
        guard_time = float(win.getProperty(RUNNING) or "0")
    except ValueError:
        guard_time = 0.0
    if guard_time and now - guard_time < TAKEOVER_GRACE:
        return
    token = repr(now)
    win.setProperty(RUNNING, token)
    log("home-daemon: started")
    # Reload keymaps so updates go live with the takeover.
    try:
        xbmc.executebuiltin("ReloadKeymaps")
    except Exception:
        pass
    monitor = xbmc.Monitor()
    was = _is_playing()
    last_file = ""
    last_paused = False
    osd_logged = False
    osd_advance_until = 0.0
    osd_advance_seen = False
    osd_was_active = False
    osd_open_until = 0.0
    osd_focus_until = 0.0
    last_vol = get_volume()
    last_photo_pub = 0
    photo_pre_idx = None
    last_drives_sig = None
    pad_last = None
    pad_pushed = None
    stop_focus_at = 0.0
    slider_seen = {}
    resume_path = None
    follow_path = ""
    follow_idx = -1
    follow_stop = 0.0
    footer_focus = ""
    resume_t = 0.0
    resume_total = 0.0
    resume_was_playing = False
    tick = 0
    play_state = _is_playing()
    settle_until = 0.0
    while not monitor.abortRequested():
        # 0.05 s base tick: photo progress runs every tick (20 Hz), other
        # duties on even ticks (10 Hz, see the "tick % 2" gate below).
        if monitor.waitForAbort(0.05):
            break
        tick += 1
        # Pre-exit handshake: our invoker has no addon context, so Kodi's
        # stop() never delivers abort -- exit on bp.exit to beat the 5s kill.
        if win.getProperty("bp.exit") == "1":
            log("home-daemon: bp.exit set, exiting")
            break
        if win.getProperty(RUNNING) != token:
            log("home-daemon: newer instance took over, exiting")
            break
        # Mute label-polling duties around a player start/stop (crash trap,
        # see PLAYER_SETTLE).
        try:
            playing_now = _is_playing()
        except Exception:
            playing_now = play_state
        if playing_now != play_state:
            play_state = playing_now
            settle_until = time.time() + PLAYER_SETTLE
        in_transition = time.time() < settle_until
        # Remember the last focused footer control: System.CurrentControlId
        # reads empty right at a track change, so the list-follow restores from
        # this instead of leaving focus stranded in the list.
        try:
            cur = xbmc.getInfoLabel("System.CurrentControlId") or ""
            if cur.isdigit() and 336 <= int(cur) <= 359:
                footer_focus = cur
        except Exception:
            pass
        # Fast duty: photo slideshow progress (20 Hz) only while the OSD is
        # visible; runs before auto-advance so the bar hits 100 at the due edge.
        try:
            photo_playing = (win.getProperty("bp.photo") == "open"
                             and win.getProperty("bp.photo.playing") == "1")
            if photo_playing and win.getProperty("bp.photo.osd") == "1":
                try:
                    due = float(win.getProperty("bp.photo.next") or "0")
                except ValueError:
                    due = 0.0
                try:
                    iv = float(win.getProperty("bp.photo.interval") or "5")
                except ValueError:
                    iv = 5.0
                now = time.time()
                pct = 100 if (due and now >= due) else photo_progress(due, iv, now)
                if pct != last_photo_pub:
                    last_photo_pub = pct
                    xbmc.executebuiltin("Skin.SetString(photo.progress,%d)" % pct)
            elif not photo_playing and last_photo_pub != 0:
                last_photo_pub = 0
                xbmc.executebuiltin("Skin.SetString(photo.progress,0)")
        except Exception as e:
            log("home-daemon error (photo progress): %s" % e)
        # Odd sub-ticks fed only the 20 Hz progress duty; skip the 10 Hz duties.
        if tick % 2 != 0:
            continue
        # Resume: remember the playing position (video + audio). Saved on stop
        # or track change, dropped when watched to the end. Uses Player.getTime/
        # getPlayingFile (not getInfoLabel -- the transition crash trap).
        try:
            if playing_now and not in_transition:
                p = xbmc.Player()
                pth = p.getPlayingFile()
                if pth:
                    if resume_path and resume_path != pth:
                        resume.store(resume_path, resume_t, resume_total)
                    resume_path = pth
                    resume_t = p.getTime()
                    resume_total = p.getTotalTime()
                    resume_was_playing = True
            elif resume_was_playing and not playing_now:
                if resume_path:
                    resume.store(resume_path, resume_t, resume_total)
                resume_path = None
                resume_was_playing = False
        except Exception as e:
            log("home-daemon error (resume save): %s" % e)
        # Duty: the list follows the playing track (audio footer only). The first
        # track is skipped so focusplay keeps the footer focus; overlays pause it.
        try:
            if playing_now:
                follow_stop = 0.0
                pth = xbmc.Player().getPlayingFile()
                if pth and pth != follow_path:
                    has_audio = xbmc.getCondVisibility("Player.HasAudio + !Player.HasVideo")
                    ov = overlay_open(win)
                    # Not gated on in_transition: a track change does not stop the
                    # player, yet the settle window is still set, which used to
                    # swallow most follows.
                    if has_audio and not ov:
                        if follow_path:
                            follow_idx = _focus_list_on(pth, footer_focus, follow_idx)
                        follow_path = pth
                    else:
                        log("home-daemon: follow skipped audio=%s overlay=%s"
                            % (has_audio, ov))
            elif follow_path:
                # The playing flag blinks false for a tick during a track change;
                # only forget the followed track once playback has really stopped,
                # else the next change counts as the "first track" and is skipped.
                if follow_stop == 0.0:
                    follow_stop = time.time()
                elif time.time() - follow_stop > PLAYER_SETTLE:
                    follow_path = ""
                    follow_idx = -1
                    follow_stop = 0.0
        except Exception as e:
            log("home-daemon error (list follow): %s" % e)
        # Duty 5: padding-row skip, 10 Hz, only while the list (33) has focus.
        try:
            if not in_transition and not playing_now \
                    and xbmc.getCondVisibility("Control.HasFocus(33)"):
                try:
                    pad_idx = int(xbmc.getInfoLabel("Container(33).CurrentItem") or "0") or None
                except Exception:
                    pad_idx = None
                try:
                    pad_hit = xbmc.getInfoLabel("Container(33).ListItem.Property(bp.padding)") == "1"
                except Exception:
                    pad_hit = False
                action, pad_last, pad_pushed = padding_step(pad_hit, pad_idx, pad_last, pad_pushed)
                if action:
                    xbmc.executebuiltin("Action(%s)" % action)
            else:
                pad_last = None
                pad_pushed = None
        except Exception as e:
            log("home-daemon error (padding): %s" % e)
        # Duty 6: one-shot focus return to list 33 after a playback stop
        # (Home active, no overlay, focus still on a hidden player-UI control).
        try:
            if stop_focus_at and not _is_playing():
                if time.time() - stop_focus_at > STOP_FOCUS_WINDOW:
                    stop_focus_at = 0.0
                elif xbmc.getCondVisibility("Window.IsActive(Home)") \
                        and not overlay_open(win) \
                        and _stop_focus_stuck():
                    xbmc.executebuiltin("SetFocus(33)")
                    log("home-daemon: focus returned to list after stop")
                    stop_focus_at = 0.0
        except Exception as e:
            log("home-daemon error (stop focus): %s" % e)
        # Fast duty: hide the audio loading overlay (bp.aload, set by fileops
        # before play). A large network file buffers for ~15 s before
        # Player.HasAudio flips, so keep the spinner until playback really
        # starts; clear early only when the attempt ended (no media), with a
        # hard cap as the last resort.
        try:
            if win.getProperty("bp.aload") == "1":
                try:
                    aload_t = float(win.getProperty("bp.aload.t") or "0")
                except ValueError:
                    aload_t = 0.0
                aload_el = time.time() - aload_t
                has_media = xbmc.getCondVisibility("Player.HasMedia")
                if ((_is_playing() and aload_el > 2.0)
                        or (aload_el > 6.0 and not has_media)
                        or aload_el > 60.0):
                    win.clearProperty("bp.aload")
                    win.clearProperty("bp.aload.t")
        except Exception as e:
            log("home-daemon error (audio loading): %s" % e)
        # Fast duty: release the list-loading veil (bp.listload) once
        # Container(33) stopped updating; timeout as last resort.
        try:
            if win.getProperty("bp.listload") == "1":
                try:
                    ll_t = float(win.getProperty("bp.listload.t") or "0")
                except ValueError:
                    ll_t = 0.0
                ready = win.getProperty("bp.listload.ready") == "1"
                try:
                    updating = xbmc.getCondVisibility("Container(33).IsUpdating")
                except Exception:
                    updating = False
                if (ready and not updating) or time.time() - ll_t > 120.0:
                    win.clearProperty("bp.listload")
                    win.clearProperty("bp.listload.t")
                    win.clearProperty("bp.listload.ready")
        except Exception as e:
            log("home-daemon error (list loading): %s" % e)
        # Duty: one deferred list refresh after a network paste -- cloud/WebDAV
        # servers often list a just-uploaded file late (CloudMe 502/503), so the
        # immediate refresh misses it; a moment later it shows without a manual
        # re-navigation.
        try:
            pr = win.getProperty("bp.pasterefresh")
            if pr:
                try:
                    due = float(pr)
                except ValueError:
                    due = 0.0
                if time.time() >= due:
                    win.clearProperty("bp.pasterefresh")
                    same = (win.getProperty("bp.pasterefresh.path")
                            == win.getProperty("bp.path"))
                    win.clearProperty("bp.pasterefresh.path")
                    if same and xbmc.getCondVisibility("Control.HasFocus(33)"):
                        win.setProperty("bp.refresh", str(time.time()))
                        xbmc.executebuiltin("Container.Refresh")
                        log("home-daemon: deferred network paste refresh")
        except Exception as e:
            log("home-daemon error (paste refresh): %s" % e)
        # Fast duty: photo slideshow auto-advance once bp.photo.next passes.
        try:
            if win.getProperty("bp.photo") == "open" \
                    and win.getProperty("bp.photo.playing") == "1" \
                    and win.getProperty("bp.info") != "open":
                try:
                    due = float(win.getProperty("bp.photo.next") or "0")
                except ValueError:
                    due = 0.0
                if due and time.time() >= due:
                    fileops.photo_step(1)
        except Exception as e:
            log("home-daemon error (photo slideshow): %s" % e)
        # Fast duty: keep one preload in flight while the slideshow runs, once
        # per index (the manual open press cannot preload).
        try:
            if win.getProperty("bp.photo") == "open" \
                    and win.getProperty("bp.photo.playing") == "1":
                try:
                    pidx = int(win.getProperty("bp.photo.idx") or "0")
                except ValueError:
                    pidx = 0
                if pidx != photo_pre_idx:
                    photo_pre_idx = pidx
                    fileops.photo_keep_preloaded()
            else:
                photo_pre_idx = None
        except Exception as e:
            log("home-daemon error (photo preload): %s" % e)
        # Fast duty: flip the Ken Burns radial corner once per cycle at the
        # image centre (all variants coincide there).
        try:
            fileops.photo_kb_tick()
        except Exception as e:
            log("home-daemon error (photo kb): %s" % e)
        # Fast duty: photo OSD auto-hide after 7 s idle; hidden OSD parks focus
        # on backdrop 899 so any input reveals it (last button remembered).
        try:
            if win.getProperty("bp.photo") == "open":
                visible = win.getProperty("bp.photo.osd") == "1"
                if xbmc.getCondVisibility("System.IdleTime(7)"):
                    # Only hide once the OSD itself has been up for the whole
                    # idle window -- a just-shown OSD must not flash away (idle
                    # is often already >7 s at the moment it is shown).
                    try:
                        t0 = float(win.getProperty("bp.photo.osd.t0") or 0)
                    except (TypeError, ValueError):
                        t0 = 0.0
                    if visible and (t0 <= 0.0 or time.time() - t0 >= 7):
                        try:
                            cid = xbmc.getInfoLabel("System.CurrentControlId")
                        except Exception:
                            cid = ""
                        if cid in ("900", "901", "902", "903", "443", "444", "445", "446", "447"):
                            win.setProperty("bp.photo.osd.focus", cid)
                        win.clearProperty("bp.photo.osd")
                        xbmc.executebuiltin("SetFocus(899)")
                elif not visible:
                    fileops._photo_osd_on(win)
                    xbmc.executebuiltin(
                        "SetFocus(%s)" % (win.getProperty("bp.photo.osd.focus") or "901"))
        except Exception as e:
            log("home-daemon error (photo osd): %s" % e)
        # Duty 8: restore focus (hamburger 32) after the settings dialog closes
        # (Home onload does not re-run behind a dialog).
        try:
            ret = win.getProperty("bp.return.focus")
            if ret and ret.isdigit() and xbmc.getCondVisibility("Window.IsActive(Home)") \
                    and not overlay_open(win):
                win.clearProperty("bp.return.focus")
                time.sleep(0.15)
                xbmc.executebuiltin("SetFocus(%s)" % ret)
                log("home-daemon: settings return focus -> %s" % ret)
        except Exception as e:
            log("home-daemon error (return focus): %s" % e)
        # Fast duty: read the input slider (400/410, no <info>) every sub-tick
        # while playing and map it through volume.py (first sight records only).
        try:
            if _is_playing():
                for sid in VOLUME_SLIDER_IDS:
                    v = _slider_percent(sid)
                    if v < 0:
                        continue
                    if sid not in slider_seen:
                        slider_seen[sid] = v
                    elif v != slider_seen[sid]:
                        slider_seen[sid] = v
                        volume.set_level(v)
        except Exception as e:
            log("home-daemon error (volume slider): %s" % e)
        # Slow duties: 20 Hz base tick -> % 20 keeps the former 1 s cadence.
        if tick % 20 != 0:
            continue
        # Duty: hotplug watch -- our drive list only rebuilds on Home
        # activation, so refresh when the signature changes.
        try:
            if win.getProperty("bp.photo") == "open":
                pass
            else:
                sig = sources.hotplug_signature()
                if last_drives_sig is None:
                    last_drives_sig = sig
                elif sig != last_drives_sig:
                    last_drives_sig = sig
                    log("home-daemon: drive set changed, refreshing")
                    xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,drives)")
        except Exception as e:
            log("home-daemon: drive watch failed: %s" % e)
        # Duty 7: key-code scan -- route the captured key into bp.rscan.code/
        # display (3 s timeout) or add it directly, then disarm.
        try:
            if win.getProperty("bp.scan.armed") == "1":
                ui = win.getProperty("bp.rscan") == "open"
                fn = win.getProperty("bp.scan.fn") or ""
                raw = scan_last_key_raw(win.getProperty("bp.scan.since") or "")
                timeout = 3.0 if ui else 15.0
                timed_out = False
                try:
                    timed_out = (time.time() - float(win.getProperty("bp.scan.t") or 0)) > timeout
                except (TypeError, ValueError):
                    timed_out = False
                if raw:
                    if ui:
                        win.setProperty("bp.rscan.code", raw)
                        win.setProperty("bp.rscan.display", raw)
                        win.clearProperty("bp.rscan.scanning")
                        win.setProperty("bp.rscan.start", xbmc.getLocalizedString(31396))
                        log("home-daemon: scan captured '%s' (scanner UI)" % raw)
                    elif fn:
                        ok, err = remotes.add_key(fn, raw)
                        if ok:
                            if remotes.build():
                                sync.keymaps()
                            xbmc.executebuiltin("ReloadKeymaps")
                            try:
                                import main
                                main.remote_open()
                            except Exception as e:
                                log("home-daemon: remote refresh failed: %s" % e)
                            log("home-daemon: remote key '%s' -> %s" % (raw, fn))
                        else:
                            log("home-daemon: remote key rejected (%s): %s" % (raw, err))
                    else:
                        win.setProperty("bp.scan.code", raw)
                    win.clearProperty("bp.scan.armed")
                    win.clearProperty("bp.scan.fn")
                elif timed_out:
                    if ui:
                        # keep the header on timeout
                        win.clearProperty("bp.rscan.scanning")
                        win.setProperty("bp.rscan.start", xbmc.getLocalizedString(31396))
                    win.clearProperty("bp.scan.armed")
                    win.clearProperty("bp.scan.fn")
                    log("home-daemon: scan timed out")
                if win.getProperty("bp.scan.armed") != "1" and win.getProperty("bp.scan.debugon") == "1":
                    xbmc.executebuiltin("ToggleDebug")
                    win.clearProperty("bp.scan.debugon")
        except Exception as e:
            log("home-daemon error (scan): %s" % e)
        # --- duty 1+2: OSD line sync + VideoOSD auto-close/pause-open
        try:
            is_playing = _is_playing()
            if is_playing:
                try:
                    current = xbmc.Player().getPlayingFile()
                    if current and current != last_file:
                        is_video = bool(xbmc.getCondVisibility("Player.HasVideo"))
                        advanced = was and bool(last_file) and is_video
                        prefix = "bp.video" if is_video else "bp.audio"
                        fileops._set_player_osd_lines(prefix, current)
                        last_file = current
                        # Track network source: audio prev/next route through
                        # the loading overlay only then.
                        if sources.is_network_path(current):
                            win.setProperty("bp.netplay", "1")
                        else:
                            win.clearProperty("bp.netplay")
                        _prefetch_metadata(current)
                        log("home-daemon: OSD lines -> %s" % redact(current))
                        if advanced:
                            # Playlist auto-advance: surface the VideoOSD; the
                            # open timestamp suppresses the idle close below.
                            try:
                                xbmc.executebuiltin("ActivateWindow(VideoOSD)")
                                now_adv = time.time()
                                osd_advance_until = now_adv + OSD_ADVANCE_WINDOW
                                osd_advance_seen = bool(xbmc.getCondVisibility("Window.IsActive(VideoOSD)"))
                                osd_focus_until = osd_advance_until
                                log("home-daemon: VideoOSD opened (advance)")
                            except Exception:
                                pass
                except Exception:
                    pass
                # Auto-close the VideoOSD after ~7 s without input (our
                # ActivateWindow-opened OSD has no core auto-close).
                try:
                    if xbmc.getCondVisibility("Window.IsActive(VideoOSD)"):
                        # Fresh open: hold the OSD for a full window from now,
                        # so the start-up/refresh delay cannot shorten it.
                        if not osd_was_active:
                            osd_open_until = time.time() + OSD_ADVANCE_WINDOW
                            log("home-daemon: VideoOSD open window %.0fs"
                                % OSD_ADVANCE_WINDOW)
                        osd_was_active = True
                        osd_advance_seen = osd_advance_seen or osd_advance_until > 0
                        if time.time() >= osd_advance_until \
                                and time.time() >= osd_open_until \
                                and xbmc.getCondVisibility("System.IdleTime(7)"):
                            xbmc.executebuiltin("Action(Back)")
                            if not osd_logged:
                                log("home-daemon: VideoOSD auto-closed (7s idle)")
                            osd_logged = True
                    else:
                        osd_logged = False
                        osd_was_active = False
                        osd_open_until = 0.0
                        if osd_advance_seen:
                            osd_advance_until = 0.0
                            osd_advance_seen = False
                        elif osd_advance_until and time.time() >= osd_advance_until:
                            osd_advance_until = 0.0
                except Exception:
                    pass
                # Pause -> surface the VideoOSD once on the playing->paused
                # transition (closing it while paused does not re-open it).
                try:
                    paused_now = bool(xbmc.getCondVisibility("Player.Paused"))
                    if (paused_now and not last_paused
                            and xbmc.getCondVisibility("Player.HasVideo")
                            and not xbmc.getCondVisibility("Window.IsActive(VideoOSD)")):
                        xbmc.executebuiltin("ActivateWindow(VideoOSD)")
                        log("home-daemon: VideoOSD opened (pause)")
                    last_paused = paused_now
                except Exception:
                    pass
                # Advance focus to 602 so arrow navigation works on the
                # auto-shown OSD; one attempt.
                try:
                    if osd_focus_until:
                        if time.time() >= osd_focus_until:
                            osd_focus_until = 0.0
                        elif xbmc.getCondVisibility("Window.IsActive(VideoOSD)"):
                            engaged = False
                            try:
                                for tid in fileops.VIDEO_FOCUS_IDS:
                                    if xbmc.getCondVisibility("Control.HasFocus(%d)" % tid):
                                        engaged = True
                                        break
                            except Exception:
                                engaged = True
                            if not engaged:
                                xbmc.executebuiltin("SetFocus(602)")
                                log("home-daemon: VideoOSD focus 602 (advance)")
                            osd_focus_until = 0.0
                except Exception:
                    pass
            if was and not is_playing:
                # Playback stopped: reset per-session states and arm the
                # one-shot focus return (duty 6).
                last_file = ""
                win.clearProperty("bp.netplay")
                last_paused = False
                stop_focus_at = time.time()
                osd_advance_until = 0.0
                osd_advance_seen = False
                osd_focus_until = 0.0
            if is_playing:
                stop_focus_at = 0.0
            was = is_playing
        except Exception as e:
            log("home-daemon error (osd): %s" % e)
        # Duty 3: volume mirror (observation only); parked while the photo
        # viewer is open (the meter catches up on close).
        try:
            if tick % VOLUME_TICKS == 0 and not in_transition \
                    and win.getProperty("bp.photo") != "open":
                cur_vol = get_volume()
                if cur_vol >= 0:
                    if cur_vol != last_vol:
                        log("home-daemon: volume changed %d -> %d" % (last_vol, cur_vol))
                        last_vol = cur_vol
                    # External change: let the meter follow it.
                    volume.resync(cur_vol)
                # External mute (remote/CEC/JSON-RPC): keep the icons in sync.
                muted = get_muted()
                if muted is not None:
                    volume.publish_mute(muted)
        except Exception as e:
            log("home-daemon error (volume): %s" % e)
        # --- duty 4: PVR dialog guard while the keyboard is open
        try:
            if not in_transition and is_kb_open(win) and is_pvr_dialog_visible():
                log("home-daemon: PVR dialog while keyboard open -> close")
                try:
                    xbmc.executebuiltin("Dialog.Close(12002)")
                except Exception:
                    pass
                try:
                    xbmc.executebuiltin("Dialog.Close(10100)")
                except Exception:
                    pass
                try:
                    xbmc.executebuiltin("Action(back)")
                except Exception:
                    pass
        except Exception as e:
            log("home-daemon error (pvr): %s" % e)
    # Clear the guard only when we still own the token.
    if win.getProperty(RUNNING) == token:
        win.clearProperty(RUNNING)
    log("home-daemon: stopped")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log("home-daemon error: %s" % e)
        except Exception:
            pass

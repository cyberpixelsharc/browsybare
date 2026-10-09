#!/usr/bin/env python3
"""Dispatcher for the Browsybare skin commands (XML/scripts call main.py,<cmd>);
runtime files live in special://userdata/skindev/, implementations in sibling modules."""
import json
import os
import re
import sys
import time

import xbmc
import xbmcgui
import xbmcvfs

import sources
import resume

from common import log, state_file, read_json, write_json, path_enc, path_dec, safe_label, redact, focus_control, skin_root, skin_name
from urllib.parse import unquote_to_bytes
from navigation import current_source_root, set_current, nav, up, root, goto, reset_top


# Lazy imports: heavy modules load only for the commands that need them.
def _run_sync():
    from sync import sync as _f
    return _f()


def _run_accents():
    from sync import accents as _f
    return _f()


def _run_theme_next():
    from sync import theme_next as _f
    return _f()


def _accent_level(level):
    from sync import set_accent_level as _f
    return _f(level)


def _musiczviz_addons():
    """[(id, name)] of the enabled visualisation add-ons, sorted by name."""
    try:
        resp = xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "Addons.GetAddons",
            "params": {"type": "xbmc.player.musicviz", "enabled": True,
                       "properties": ["name"]}}))
        addons = json.loads(resp).get("result", {}).get("addons", []) or []
    except Exception:
        return []
    out = [(a.get("addonid") or "", a.get("name") or a.get("addonid") or "")
           for a in addons]
    out = [x for x in out if x[0]]
    out.sort(key=lambda x: x[1].lower())
    return out


def _active_viz():
    """The selected visualisation add-on id (musicplayer.visualisation), or ""."""
    try:
        resp = xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "Settings.GetSettingValue",
            "params": {"setting": "musicplayer.visualisation"}}))
        return json.loads(resp).get("result", {}).get("value") or ""
    except Exception:
        return ""


def _set_active_viz(vid):
    try:
        xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "Settings.SetSettingValue",
            "params": {"setting": "musicplayer.visualisation", "value": vid}}))
    except Exception:
        pass


def _viz_label(vid, vizzes):
    if not vid:
        return xbmc.getLocalizedString(31548)   # "Off"
    for aid, name in vizzes:
        if aid == vid:
            return name
    return vid


def viz_init():
    """Publish the current visualisation name for the settings row."""
    win = xbmcgui.Window(10000)
    vizzes = _musiczviz_addons()
    enabled = xbmc.getCondVisibility("Skin.HasSetting(audio.visualisation)")
    vid = _active_viz() if enabled else ""
    win.setProperty("bp.viz.name", safe_label(_viz_label(vid, vizzes)))


def vizcycle():
    """Cycle the audio visualisation: Off -> each installed add-on -> Off."""
    win = xbmcgui.Window(10000)
    vizzes = _musiczviz_addons()
    if not vizzes:
        try:
            xbmcgui.Dialog().notification(
                skin_name(),
                safe_label(xbmc.getLocalizedString(31549)),
                xbmcgui.NOTIFICATION_WARNING, 4000)
        except Exception:
            pass
        xbmc.executebuiltin("Skin.Reset(audio.visualisation)")
        win.setProperty("bp.viz.name", xbmc.getLocalizedString(31548))
        return
    ids = [""] + [v[0] for v in vizzes]
    cur = _active_viz()
    if (not xbmc.getCondVisibility("Skin.HasSetting(audio.visualisation)")
            or cur not in ids):
        i = 0
    else:
        i = ids.index(cur)
    nxt = ids[(i + 1) % len(ids)]
    _set_active_viz(nxt)
    if nxt:
        xbmc.executebuiltin("Skin.SetBool(audio.visualisation)")
    else:
        xbmc.executebuiltin("Skin.Reset(audio.visualisation)")
    win.setProperty("bp.viz.name", safe_label(_viz_label(nxt, vizzes)))
    log("visualisation cycle: %s" % (nxt or "off"))


def vizsettings():
    """V key in the visualisation window: toggle the current visualisation
    add-on's settings dialog (Kodi's default V only opens it)."""
    if xbmc.getCondVisibility("Window.IsActive(10140)"):
        xbmc.executebuiltin("Dialog.Close(10140)")
        return
    xbmc.executebuiltin("Addon.Default.OpenSettings(xbmc.player.musicviz)")


def _kmaps():
    from sync import keymaps as _f
    return _f()


def _fopen(path):
    from fileops import open_file as _f
    return _f(path)


def _resume_clear(win):
    for p in ("bp.resume", "bp.resume.path", "bp.resume.t", "bp.resume.line"):
        win.clearProperty(p)


def resumeyes():
    """Continue-watching prompt: resume at the saved position (Fortsetzen)."""
    win = xbmcgui.Window(10000)
    path = path_dec(win.getProperty("bp.resume.path"))
    try:
        pos = float(win.getProperty("bp.resume.t") or "0")
    except (TypeError, ValueError):
        pos = 0.0
    _resume_clear(win)
    if path and pos > 0:
        from fileops import open_file
        open_file(path, resume_pos=pos)
        log("resume: play from %.0fs" % pos)


def resumeno():
    """Continue-watching prompt: play from the start, forget the position."""
    win = xbmcgui.Window(10000)
    path = path_dec(win.getProperty("bp.resume.path"))
    _resume_clear(win)
    if path:
        try:
            resume.clear(path)
        except Exception:
            pass
        from fileops import open_file
        open_file(path)
        log("resume: play from start")


def resumecancel():
    """Continue-watching prompt dismissed without playing (backdrop / Back)."""
    _resume_clear(xbmcgui.Window(10000))
    # Focus sat on the (now hidden) modal button; hand it back to the list.
    try:
        time.sleep(0.2)
        xbmc.executebuiltin("SetFocus(33)")
    except Exception:
        pass
    log("resume: prompt cancelled")


def _fphotoclose():
    from fileops import photo_close as _f
    return _f()


def _fphotostep(delta):
    from fileops import photo_step as _f
    return _f(delta)


def _fphotoplay():
    from fileops import photo_play as _f
    return _f()


def _fphotoshow():
    from fileops import photo_show_osd as _f
    return _f()


def _fphotointerval():
    from fileops import photo_interval_cycle as _f
    return _f()


def _fphotomode():
    from fileops import photo_mode_cycle as _f
    return _f()


def _fphotorepeat():
    from fileops import photo_repeat_toggle as _f
    return _f()


def _fphotoshuffle():
    from fileops import photo_shuffle_toggle as _f
    return _f()


def _fresetopen(rubric):
    from reset import reset_open as _f
    return _f(rubric)


def _fresetrun():
    from reset import reset_run as _f
    return _f()


def _fresetclose():
    from reset import reset_close as _f
    return _f()


def _frename(path):
    from fileops import rename as _f
    return _f(path)


def _fctx(path):
    from fileops import ctx as _f
    return _f(path)


def _fctx_current():
    from fileops import ctx_current as _f
    return _f()


def _fmkdircreate():
    from fileops import mkdircreate as _f
    return _f()


def _fdelete(path):
    from fileops import delete as _f
    return _f(path)


def _fdelconfirm():
    from fileops import delconfirm as _f
    return _f()


def _fclip_copy(path):
    from fileops import clip_copy as _f
    return _f(path)


def _fclip_cut(path):
    from fileops import clip_cut as _f
    return _f(path)


def _fclip_paste():
    from fileops import clip_paste as _f
    return _f()


def _fmkdir():
    from fileops import mkdir as _f
    return _f()


def _ffocusplay():
    from fileops import focusplay as _f
    return _f()


def _ffocusvideo():
    from fileops import focusvideo as _f
    return _f()


def _faudioprev():
    from fileops import audio_prev as _f
    return _f()


def _faudionext():
    from fileops import audio_next as _f
    return _f()


def _ffocusplaying():
    from fileops import focus_playing as _f
    return _f()


def _faudioshuffle():
    from fileops import toggle_shuffle as _f, AUDIO_EXT as _e
    return _f(0, _e)


def _fvideoshuffle():
    from fileops import toggle_shuffle as _f, VIDEO_EXT as _e
    return _f(1, _e)


def _fticker(text, a, b):
    from fileops import _ticker as _f
    return _f(text, a, b)


# Lazy imports: blacklist/dirsources/remotes load on demand.
def _blacklist():
    import blacklist as _m
    return _m


def _dirsources():
    import dirsources as _m
    return _m


def _remotes():
    import remotes as _m
    return _m



_SKIP_AMOUNTS = [10, 30, 60, 180, 600]  # seconds
_SKIP_THRESHOLD = 2.0  # s between presses to accumulate


def skip(direction):
    """Accelerated skip: repeated presses increase the jump distance.
    State persists via window properties (each RunScript = new process)."""
    now = time.time()
    win = xbmcgui.Window(10000)
    prop_t = "bp.skip.%s.t" % direction
    prop_i = "bp.skip.%s.i" % direction
    try:
        prev_t = float(win.getProperty(prop_t))
        prev_i = int(win.getProperty(prop_i))
    except (ValueError, TypeError):
        prev_t = 0
        prev_i = -1
    if prev_i >= 0 and (now - prev_t) < _SKIP_THRESHOLD:
        idx = min(prev_i + 1, len(_SKIP_AMOUNTS) - 1)
    else:
        idx = 0
    win.setProperty(prop_t, str(now))
    win.setProperty(prop_i, str(idx))
    seconds = _SKIP_AMOUNTS[idx]
    if direction == "bwd":
        seconds = -seconds
    player = xbmc.Player()
    if player.isPlaying():
        new_time = max(0, player.getTime() + seconds)
        player.seekTime(new_time)
        log("skip %s %ds -> %.0fs" % ("fwd" if seconds > 0 else "bwd",
                                       abs(seconds), new_time))



_SPEED_LEVELS = [1, 2, 4, 8, 16, 32]  # Kodi integer speeds


def speed():
    """Cycle playback speed 1x..32x via JSON-RPC Player.SetSpeed.
    Stores the current speed in bp.speed for the button label."""
    win = xbmcgui.Window(10000)
    player = xbmc.Player()
    if not player.isPlaying():
        return
    try:
        cur = int(float(win.getProperty("bp.speed")))
    except (ValueError, TypeError):
        cur = 1
    try:
        idx = _SPEED_LEVELS.index(cur)
        idx = (idx + 1) % len(_SPEED_LEVELS)
    except ValueError:
        idx = 0
    new_speed = _SPEED_LEVELS[idx]
    result = xbmc.executeJSONRPC('{"jsonrpc":"2.0","id":1,"method":"Player.GetActivePlayers"}')
    try:
        players = json.loads(result).get("result", [])
        if players:
            pid = players[0]["playerid"]
            xbmc.executeJSONRPC(
                '{"jsonrpc":"2.0","id":1,"method":"Player.SetSpeed",'
                '"params":{"playerid":%d,"speed":%d}}' % (pid, new_speed))
    except (ValueError, KeyError, IndexError):
        pass
    win.setProperty("bp.speed", str(new_speed))
    log("speed -> %dx" % new_speed)


# --------------------------------------------------- display refresh delay

# Kodi's refresh-rate switch after the audio sink opens cuts the first ~1.5s of
# video; hold videoscreen.delayrefreshchange ~2s so the TV/AVR re-locks.
REFRESH_DELAY = 20  # 0.1 s units -> 2.0 s


def refreshdelay():
    """Apply/clear Kodi's refresh-change delay from the video.norefreshdelay
    option (OFF -> REFRESH_DELAY, ON -> 0); idempotent, global setting."""
    try:
        off = bool(xbmc.getCondVisibility("Skin.HasSetting(video.norefreshdelay)"))
        value = 0 if off else REFRESH_DELAY
        xbmc.executeJSONRPC(
            '{"jsonrpc":"2.0","id":1,"method":"Settings.SetSettingValue",'
            '"params":{"setting":"videoscreen.delayrefreshchange","value":%d}}' % value)
        log("refreshdelay: videoscreen.delayrefreshchange = %d" % value)
    except Exception as e:
        log("refreshdelay failed: %s" % e)



def _decode(arg):
    """Decode a double percent-encoded RunScript path argument, unquoting
    exactly twice and returning FS form (unquote to bytes, then os.fsdecode)."""
    if "%" not in arg:
        return arg
    try:
        raw = unquote_to_bytes(arg)
        raw = unquote_to_bytes(raw.decode("ascii"))
        return os.fsdecode(raw)
    except Exception:
        return arg



def _set_default_source(visible):
    """Pick and navigate to the boot default source over the VISIBLE list only
    (prefer the home folder, else a real data drive, else the first visible)."""
    win = xbmcgui.Window(10000)
    candidates = [d for d in visible if d.get("path") and os.path.isdir(d["path"])]
    home_path = (sources.home_drive() or {}).get("path", "").rstrip("/")

    def bootable(d):
        p = d["path"].rstrip("/")
        if p == home_path:
            # The user folder is the first choice on every platform; only a
            # hidden or missing home falls through to a real data drive.
            return True
        if sources.is_system_volume(d["path"]):
            return False
        return not os.path.isdir(d["path"].rstrip("/") + "/Applications")

    data = [d for d in candidates if bootable(d)]
    path = ""
    for d in (data or candidates):
        path = d["path"].rstrip("/")
        break
    if not path:
        path = os.path.expanduser("~")
    win.setProperty("bp.src", path_enc(path))
    win.setProperty("bp.title", sources.short_label(path))
    win.setProperty("bp.current.icon", sources.icon_for(path))
    set_current(path)
    return path


def _enter_no_source():
    """No visible source: empty bp.path yields a zero-item listing;
    bp.nosource switches the empty-state label."""
    win = xbmcgui.Window(10000)
    win.setProperty("bp.nosource", "1")
    win.setProperty("bp.title", xbmc.getLocalizedString(31326))
    win.clearProperty("bp.src")
    win.clearProperty("bp.net")
    set_current("")
    # set_current clears bp.current.icon, so set the no-source icon last.
    win.setProperty("bp.current.icon", "devices/exclamation-circle.png")
    log("no source: all drives hidden or none present")


def drives():
    drives = sources.all_drives()
    drivelist()
    xbmcgui.Window(10000).setProperty("bp.speed", "1")
    if xbmcgui.Window(10000).getProperty("bp.nosource"):
        log("drives: %d entries, no visible source" % len(drives))
        return
    # Home onload fires on every activation; only pick a default when bp.path
    # is empty (stale paths fall back to the default).
    cur = sources.rstrip_slash(path_dec(xbmcgui.Window(10000).getProperty("bp.path") or ""))
    # Network (VFS) paths are never os.path.isdir but must still be kept, or
    # every non-navigation Home activation jumps to the default source.
    if cur and (sources.is_network_path(cur) or os.path.isdir(cur)):
        # Re-derive breadcrumb/chip from the persisted path (else stale).
        set_current(cur)
        log("drives: %d entries, keep current path %s" % (len(drives), cur))
        return
    path = _set_default_source(sources.visible(drives))
    log("drives: %d entries, path: %s" % (len(drives), path))


def _dropdown_entries(visible):
    """Dropdown entries; while the picker is active (bp.pick.active) hide
    directory and network sources -- neither is a valid pick target."""
    try:
        if xbmcgui.Window(10000).getProperty("bp.pick.active") != "1":
            return visible
    except Exception:
        return visible
    return [d for d in visible if (d.get("type") or "") not in ("dirsource", "network")]


def drivelist():
    """Refresh drive-sources.json + bp.drive.1-10 (dropdown, filtered+compacted)
    and bp.alldrive.1-6 (sources rows); runs the no-source/hidden-drive transitions."""
    drives = sources.all_drives()
    write_json(state_file("drive-sources.json"), sources.drive_entries())
    visible = sources.visible(drives)
    win = xbmcgui.Window(10000)
    # Positional family: regular local drives only (home/system have own rows).
    regular = sources.local_regular(drives)
    home = next((d for d in drives if d.get("type") == "home"), None)
    win.setProperty("bp.home.label", safe_label(home.get("label", "")) if home else "")
    system = next((d for d in drives if d.get("system")), None)
    win.setProperty("bp.system.label", safe_label(system.get("label", "")) if system else "")
    for i in range(6):
        all_label = regular[i].get("label", "") if i < len(regular) else ""
        win.setProperty("bp.alldrive.%d" % (i + 1), safe_label(all_label))
        win.setProperty("bp.alldrive.%d.icon" % (i + 1),
                        sources.drive_icon(regular[i]) if i < len(regular) else "")
        on = "1" if (i < len(regular) and sources.drive_shown(regular[i])) else ""
        win.setProperty("bp.driveon.%d" % (i + 1), on)
    dropdown = _dropdown_entries(visible)
    for i in range(10):
        vis_label = dropdown[i].get("label", "") if i < len(dropdown) else ""
        win.setProperty("bp.drive.%d" % (i + 1), safe_label(vis_label))
        win.setProperty("bp.drive.%d.icon" % (i + 1),
                        sources.drive_icon(dropdown[i]) if i < len(dropdown) else "")
    if not visible:
        if not win.getProperty("bp.nosource"):
            _enter_no_source()
    elif win.getProperty("bp.nosource"):
        # first visible source again -> leave empty state, jump to default
        win.clearProperty("bp.nosource")
        _set_default_source(visible)
    else:
        # browsed drive was hidden -> jump to the next active drive
        cur = sources.rstrip_slash(path_dec(win.getProperty("bp.path") or ""))
        cur_root = current_source_root(cur) if cur else ""
        all_paths = [sources.rstrip_slash(d.get("path") or "") for d in drives]
        vis_paths = {sources.rstrip_slash(d.get("path") or "") for d in visible}
        if cur and cur_root in all_paths and cur_root not in vis_paths:
            nxt = sources.next_visible_after(cur_root, drives, visible)
            if nxt:
                nxt_path = sources.rstrip_slash(nxt.get("path") or "")
                win.setProperty("bp.src", path_enc(nxt_path))
                win.setProperty("bp.title", sources.short_label(nxt_path))
                win.setProperty("bp.current.icon", sources.icon_for(nxt_path))
                set_current(nxt_path)
                log("current source hidden -> jumped to %s" % nxt_path)
    log("drivelist: %d entries, %d visible" % (len(drives), len(visible)))
    # Directory-source rows ride along so the settings rubric updates live.
    dirsrc_open()
    netsrc_open()


def drive_toggle(idx):
    """Toggle a regular drive (slot 1-6) by identity: flip
    hidden.drive.<md5(path)> (unset = shown) and re-render."""
    try:
        slot = int(idx)
    except (TypeError, ValueError):
        return
    drives = sources.all_drives()
    regular = sources.local_regular(drives)
    if not 1 <= slot <= len(regular):
        return
    key = sources.drive_key(regular[slot - 1])
    try:
        if xbmc.getCondVisibility("Skin.HasSetting(hidden.drive.%s)" % key):
            xbmc.executebuiltin("Skin.Reset(hidden.drive.%s)" % key)
        else:
            xbmc.executebuiltin("Skin.SetBool(hidden.drive.%s)" % key)
    except Exception:
        return
    drivelist()
    log("drive toggle slot %d (%s)" % (slot, redact(regular[slot - 1].get("label", ""))))


def drive(idx):
    """Switch to the visible dropdown slot `idx` (1-based, compacted -- not the
    raw drive-sources.json index); resets history and lists the new root."""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return
    drives = [d for d in read_json(state_file("drive-sources.json"), [])
              if d.get("type") not in ("dirsource", "network")]
    # Merge dir/network sources live in enumeration order (same as all_drives()).
    drives = drives + sources.dirsource_drives() + sources.network_sources()
    visible = _dropdown_entries(sources.visible(drives))
    if idx < 1 or idx > len(visible):
        return
    d = visible[idx - 1]
    # rstrip_slash (not rstrip): a bare "ftp://" must keep its slashes.
    path = sources.rstrip_slash(d.get("path") or "")
    is_net = (d.get("type") or "") == "network"
    # Network sources are VFS URLs (isdir always False); path-less entries stay
    # selectable to show their connection-error row.
    if not is_net and (not path or not os.path.isdir(path)):
        return
    win = xbmcgui.Window(10000)
    win.setProperty("bp.net", "1" if is_net else "")
    # Set bp.src BEFORE set_current: current_source_root's /Volumes/ fallback
    # would otherwise resolve a USB dirsource to its drive.
    if path:
        win.setProperty("bp.src", path_enc(path))
    reset_top()
    set_current(path)
    if path:
        win.setProperty("bp.title", sources.short_label(path))
    else:
        win.setProperty("bp.title", safe_label(d.get("label") or ""))
    try:
        win.setProperty("bp.current.icon",
                        "devices/hdd-network.png" if is_net else sources.icon_for(path))
    except Exception:
        pass
    log("drive: select %d -> %s" % (idx, path or "(network, no path)"))



def loadcancel():
    """Back/ESC while the list is loading (bp.listload): abort the pending fetch
    and return to the folder we came from. An unreachable network source would
    otherwise hold the spinner until Kodi's VFS timeout."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.listload") != "1":
        return
    win.clearProperty("bp.listload")
    win.clearProperty("bp.listload.t")
    win.clearProperty("bp.listload.ready")
    prev = path_dec(win.getProperty("bp.prev.path") or "")
    if prev:
        win.clearProperty("bp.prev.path")
        log("loadcancel: back to %s" % redact(prev))
        set_current(prev)
        # set_current would have captured the abandoned path as the new prev.
        win.clearProperty("bp.prev.path")
    else:
        log("loadcancel: no previous view, list stays on the current path")



def search_refresh():
    # URL-label change alone is an unreliable reload trigger, so force a
    # Container.Refresh; new content -> cursor back to top.
    reset_top()
    xbmc.executebuiltin("Container.Refresh")



def sort_cycle():
    """Cycle Skin.String(sort) name -> size -> date; &sort= drives list.py
    sorting plus an explicit Container.Refresh."""
    order = ["name", "size", "date"]
    cur = ""
    try:
        cur = xbmc.getInfoLabel("Skin.String(sort)") or ""
        cur = cur.strip().lower()
    except Exception:
        cur = ""
    if cur not in order:
        cur = "name"
    nxt = order[(order.index(cur) + 1) % len(order)]
    try:
        xbmc.executebuiltin('Skin.SetString(sort,%s)' % nxt)
    except Exception:
        pass
    log("sort: %s -> %s" % (cur, nxt))
    xbmc.executebuiltin("Container.Refresh")



def foldersfirst():
    """Toggle folders-first setting (opt-out folders.last, default ON)."""
    xbmc.executebuiltin("Skin.ToggleSetting(folders.last)")



def grid():
    # dev grid guide lines
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.grid"):
        win.clearProperty("bp.grid")
    else:
        win.setProperty("bp.grid", "on")


def blacklist_open():
    """Open the blocked-patterns overlay (bp.bl); bp.blstd.N marks baseline
    rows (no remove), bp.blon.N the toggle state."""
    win = xbmcgui.Window(10000)
    patterns = _blacklist().load()
    seed = set(_blacklist().seed_patterns())
    off = set(_blacklist().off_patterns())
    for i in range(30):
        p = patterns[i] if i < len(patterns) else ""
        win.setProperty("bp.bl.%d" % (i + 1), p)
        win.setProperty("bp.blstd.%d" % (i + 1), "1" if p in seed else "")
        win.setProperty("bp.blon.%d" % (i + 1), "" if (p and p in off) else "1")
    win.setProperty("bp.bl", "open")
    log("blacklist overlay: %d patterns (%d off)" % (len(patterns), len(off)))


def dropdown_open():
    """Open the drive dropdown focused on the current drive row (title match,
    else first). SetProperty(open) must be last: XML onclick conditions are live."""
    win = xbmcgui.Window(10000)
    title = (win.getProperty("bp.title") or "").strip()
    slot = 0
    for n in range(1, 11):
        label = (win.getProperty("bp.drive.%d" % n) or "").strip()
        if label and label == title:
            slot = n
            break
    win.setProperty("bp.drives", "open")
    focus_control(59 + (slot or 1))
    log("dropdown open: focus slot %d (title %r)" % (slot or 1, redact(title)))


def accent_next():
    """Cycle the accent to the next slot (wrap); mirrors the swatch onclick
    (accent string + bp.accent.slot/tint, alpha 1A = 10%)."""
    win = xbmcgui.Window(10000)
    cur = (xbmc.getInfoLabel("Skin.String(accent)") or "").upper()
    colors = [(win.getProperty("bp.accent.%d" % i) or "").upper() for i in range(1, 10)]
    colors = [c for c in colors if c]
    if not colors:
        return
    try:
        idx = colors.index(cur)
    except ValueError:
        idx = -1
    nxt = colors[(idx + 1) % len(colors)]
    slot = colors.index(nxt) + 1
    xbmc.executebuiltin("Skin.SetString(accent,%s)" % nxt)
    win.setProperty("bp.accent.slot", str(slot))
    if len(nxt) == 8:
        win.setProperty("bp.accent.tint", "1A" + nxt[2:])
    hov = win.getProperty("bp.accent.hov.%d" % slot)
    if hov:
        win.setProperty("bp.accent.hov", hov)
    # Update the base-layer bg/focus now too: Home does not reload behind the
    # settings dialog.
    for key in ("bg", "bg2"):
        val = win.getProperty("bp.accent.%s.%d" % (key, slot))
        if val:
            win.setProperty("bp.accent.%s" % key, val)
    win.setProperty("bp.accent.focus", nxt)
    log("accent: slot %d (%s)" % (slot, nxt))


def intensity(idx):
    """Apply color intensity level 1-5 (settings dots, direct-apply)."""
    try:
        level = int(idx)
    except (TypeError, ValueError):
        return
    if 1 <= level <= 5:
        _accent_level("L%d" % level)
        log("intensity: L%d" % level)


def intensity_next():
    """Cycle intensity to the next level (wrap)."""
    try:
        name = (xbmc.getInfoLabel("Skin.String(accent.level)") or "L3").upper()
        level = int(name[1]) if name in ("L1", "L2", "L3", "L4", "L5") else 3
    except Exception:
        level = 3
    intensity((level % 5) + 1)


GUISOUND_VOLUMES = (0, 25, 50, 75, 100)


def settings_tab(tab, tabid):
    """Switch the settings tab and reset its shared grouplist (id 91) scroll:
    briefly focus the tab's top row, then return focus to the tab button."""
    try:
        tab = int(tab)
    except (TypeError, ValueError):
        return
    win = xbmcgui.Window(10000)
    win.setProperty("bp.settings.tab", str(tab))
    time.sleep(0.08)
    candidates = {1: (274,), 2: (272, 701), 3: (129, 105, 271),
                  4: (277,), 5: (500,)}.get(tab, ())
    reset = False
    for cid in candidates:
        try:
            xbmc.executebuiltin("SetFocus(%d)" % cid)
            time.sleep(0.03)
            if xbmc.getCondVisibility("Control.HasFocus(%d)" % cid):
                reset = True
                break
        except Exception:
            continue
    try:
        xbmc.executebuiltin("SetFocus(%s)" % tabid)
    except Exception:
        pass
    log("settings_tab: %d (scroll reset=%s)" % (tab, reset))


def guisound(idx=""):
    """Apply GUI sound level 0-4 (0 mutes, 1-4 = 25/50/75/100%); empty idx
    re-applies Skin.String(guisound.level) (boot, default 4)."""
    try:
        level = int(idx) if str(idx).strip() != "" else int(
            xbmc.getInfoLabel("Skin.String(guisound.level)") or 4)
    except (TypeError, ValueError):
        level = 4
    level = max(0, min(4, level))
    try:
        xbmc.executebuiltin("Skin.SetString(guisound.level,%d)" % level)
    except Exception:
        pass
    try:
        xbmcgui.Window(10000).setProperty("bp.guisound.level", str(level))
    except Exception:
        pass
    volume = GUISOUND_VOLUMES[level]
    mode = 0 if level == 0 else 1
    try:
        xbmc.executeJSONRPC(
            '{"jsonrpc":"2.0","id":1,"method":"Settings.SetSettingValue",'
            '"params":{"setting":"audiooutput.guisoundvolume","value":%d}}' % volume)
        xbmc.executeJSONRPC(
            '{"jsonrpc":"2.0","id":1,"method":"Settings.SetSettingValue",'
            '"params":{"setting":"audiooutput.guisoundmode","value":%d}}' % mode)
        log("guisound: level %d (volume %d, mode %d)" % (level, volume, mode))
    except Exception as e:
        log("guisound failed: %s" % e)


def guisound_next():
    """Cycle GUI sound level to the next level (wrap)."""
    try:
        level = int(xbmc.getInfoLabel("Skin.String(guisound.level)") or 4)
    except (TypeError, ValueError):
        level = 4
    guisound((level + 1) % 5)


def _bump_list():
    """Reliable list reload: bump the r URL param (bp.refresh) plus an explicit
    Container.Refresh."""
    try:
        xbmcgui.Window(10000).setProperty("bp.refresh", str(time.time()))
        xbmc.executebuiltin("Container.Refresh")
    except Exception:
        pass


def _blacklist_row_id(slot):
    """Row control id for a 1-based blacklist slot (ids are irregular)."""
    if slot <= 3:
        return 700 + slot
    if slot == 4:
        return 184
    if slot <= 6:
        return 699 + slot
    return 180 + slot


def blacklist_add():
    """Keyboard-OK: add bp.blacklist.new, or replace bp.blacklist.edit."""
    win = xbmcgui.Window(10000)
    new = (win.getProperty("bp.blacklist.new") or "").strip()
    mode = win.getProperty("bp.kb.mode") or ""
    try:
        edit = int(win.getProperty("bp.blacklist.edit") or 0)
    except (TypeError, ValueError):
        edit = 0
    win.clearProperty("bp.blacklist.new")
    win.clearProperty("bp.blacklist.edit")
    if not new:
        return
    if mode == "blacklistedit" and edit:
        if _blacklist().replace(edit, new):
            blacklist_open()
            _bump_list()
            if 1 <= edit <= 30:
                time.sleep(0.25)
                xbmc.executebuiltin("SetFocus(%d)" % _blacklist_row_id(edit))
            log("blacklist: edited slot %d -> '%s'" % (edit, new))
        return
    if _blacklist().add(new):
        blacklist_open()
        _bump_list()
        try:
            slot = [p.lower() for p in _blacklist().load()].index(new.lower()) + 1
        except ValueError:
            slot = 0
        if 1 <= slot <= 30:
            time.sleep(0.25)
            xbmc.executebuiltin("SetFocus(%d)" % _blacklist_row_id(slot))
        log("blacklist: added '%s' (slot %d)" % (new, slot))


def blacklist_remove(idx):
    """Remove the 1-based pattern (row click) and re-render."""
    win = xbmcgui.Window(10000)
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return
    if _blacklist().remove(idx):
        blacklist_open()
        _bump_list()


def blacklist_toggle(idx):
    """Row toggle: enable/disable slot idx by NAME (slots shift)."""
    win = xbmcgui.Window(10000)
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return
    pattern = (win.getProperty("bp.bl.%d" % idx) or "").strip()
    if not pattern:
        return
    off = _blacklist().toggle(pattern)
    blacklist_open()
    _bump_list()
    log("blacklist: %s %r" % ("disabled" if off else "enabled", pattern))


def dirsrc_open():
    """Render the directory-source rows (bp.dirsrc.1-10); unavailable sources
    render in parentheses (the dropdown filters them out)."""
    win = xbmcgui.Window(10000)
    sources_list = _dirsources().load()
    _migrate_dirsource_hide(sources_list)
    for i in range(10):
        raw = sources_list[i] if i < len(sources_list) else ""
        disp = sources.display_path(raw)
        if raw and not os.path.isdir(raw):
            disp = "( %s )" % disp
        win.setProperty("bp.dirsrc.%d" % (i + 1), disp)
        # Row toggle state by hash identity (hide.dirsource.<key>).
        key = sources.dirsource_key(raw) if raw else ""
        on = ""
        if key:
            try:
                on = "" if xbmc.getCondVisibility(
                    "Skin.HasSetting(hide.dirsource.%s)" % key) else "1"
            except Exception:
                on = "1"
        win.setProperty("bp.dirsrc.%d.on" % (i + 1), on)
    log("dirsources: %d entries" % len(sources_list))


def _migrate_dirsource_hide(sources_list):
    """One-time: migrate legacy numeric hide.dirsource.<slot> keys to the path
    hash; resets the old keys. Idempotent."""
    for i, raw in enumerate(sources_list[:10]):
        if not raw:
            continue
        slot = i + 1
        try:
            if xbmc.getCondVisibility("Skin.HasSetting(hide.dirsource.%d)" % slot):
                key = sources.dirsource_key(raw)
                xbmc.executebuiltin("Skin.SetBool(hide.dirsource.%s)" % key)
                xbmc.executebuiltin("Skin.Reset(hide.dirsource.%d)" % slot)
                log("dirsource hide migrated: slot %d -> %s" % (slot, key))
        except Exception:
            pass


def dirsrc_remove(idx):
    """Remove the 1-based source (row click) and re-render."""
    win = xbmcgui.Window(10000)
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return
    if _dirsources().remove(idx):
        dirsrc_open()
        drivelist()


def netsrc_open():
    """Render the network-source rows (bp.net.1-10, label only); visibility
    rides the regular drive toggle (hidden.drive.<md5(path)>)."""
    win = xbmcgui.Window(10000)
    entries = sources.netsrc_load()
    for i in range(sources.NETSRC_ROWS):
        e = entries[i] if i < len(entries) else None
        win.setProperty("bp.net.%d" % (i + 1), safe_label(e["label"]) if e else "")
        on = ""
        if e:
            try:
                on = "1" if sources.netsrc_shown(e, i + 1) else ""
            except Exception:
                on = "1"
        win.setProperty("bp.net.%d.on" % (i + 1), on)
    log("netsources: %d entries" % len(entries))


def netcommit():
    """Keyboard-OK (step 2): combine bp.net.pending with the name and add it."""
    win = xbmcgui.Window(10000)
    url = (win.getProperty("bp.net.pending") or "").strip()
    label = (win.getProperty("bp.net.label") or "").strip()
    win.clearProperty("bp.net.pending")
    win.clearProperty("bp.net.label")
    if not url:
        return
    if sources.netsrc_add(label, url):
        netsrc_open()
        drivelist()
        try:
            slot = [e["path"].lower() for e in sources.netsrc_load()].index(url.lower()) + 1
        except ValueError:
            slot = 0
        if 1 <= slot <= sources.NETSRC_ROWS:
            time.sleep(0.25)
            xbmc.executebuiltin("SetFocus(%d)" % (320 + slot))
        log("netsource: added '%s' (%s)" % (label, redact(url)))
    else:
        time.sleep(0.25)
        xbmc.executebuiltin("SetFocus(273)")
        log("netsource: rejected '%s'" % redact(url))


NET_PROTOCOLS = ("ftp", "ftps", "sftp", "smb", "nfs", "dav", "davs")


def _netsrc_set_ro(win, scheme):
    """Publish whether the editor's scheme is read-only (ftp/ftps/sftp cannot
    write through Kodi's VFS), so the XML greys the write toggle."""
    key = (scheme or "").split("://", 1)[0].strip().lower() + "://"
    ro = key in sources.RO_SCHEMES
    try:
        win.setProperty("bp.netsrc.ro", "1" if ro else "")
        if ro:
            win.setProperty("bp.netsrc.write", "")  # read-only: never writable
    except Exception:
        pass


def _netsrc_clear_editor():
    win = xbmcgui.Window(10000)
    win.clearProperty("bp.netsrc")
    for p in ("name", "proto", "scheme", "server", "port", "path", "user", "pass", "pass.mask", "test", "edit", "write"):
        win.clearProperty("bp.netsrc." + p)


def netro_toggle():
    """Toggle the editor's write-access flag (off = read-only)."""
    win = xbmcgui.Window(10000)
    cur = "1" if win.getProperty("bp.netsrc.write") == "1" else ""
    win.setProperty("bp.netsrc.write", "" if cur == "1" else "1")
    log("netsource: write %s" % ("off" if cur == "1" else "on"))


def _netsrc_notify(string_id, error=True, value=""):
    try:
        msg = xbmc.getLocalizedString(string_id)
        if value:
            msg = "%s (%s)" % (msg, value)
        xbmcgui.Dialog().notification(
            xbmc.getLocalizedString(31448) or skin_name(),
            safe_label(msg),
            xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO, 4000)
    except Exception:
        pass


# Schemes whose Kodi VFS handler is a separate add-on that must be installed.
_VFS_ADDON = {"sftp": "vfs.sftp"}


def _missing_vfs_addon(scheme):
    """The add-on id `scheme` needs but which is missing/disabled, or ""."""
    addon = _VFS_ADDON.get((scheme or "").split("://", 1)[0].strip().lower())
    if addon:
        try:
            if not xbmc.getCondVisibility("System.AddonIsEnabled(%s)" % addon):
                return addon
        except Exception:
            pass
    return ""


def netsrcnew(idx=None):
    """Open the network-source editor modal; with `idx` (1-based) it is
    pre-filled and OK replaces that entry."""
    win = xbmcgui.Window(10000)
    try:
        edit = int(idx) if idx else 0
    except (TypeError, ValueError):
        edit = 0
    entries = sources.netsrc_load()
    if not (1 <= edit <= len(entries)):
        edit = 0
    e = entries[edit - 1] if edit else None
    scheme, user, passwd, server, port, sub = ("ftp", "", "", "", "", "")
    if e:
        # The stored editor fields are the source of truth and are kept verbatim.
        f = e.get("fields") or {}
        scheme = f.get("scheme") or "ftp"
        user = f.get("user") or ""
        passwd = f.get("pass") or ""
        server = f.get("server") or ""
        port = f.get("port") or ""
        sub = f.get("path") or ""
    win.setProperty("bp.netsrc.edit", str(edit) if edit else "")
    win.setProperty("bp.netsrc.name", e["label"] if e else "")
    win.setProperty("bp.netsrc.proto", scheme.upper())
    win.setProperty("bp.netsrc.scheme", scheme + "://")
    win.setProperty("bp.netsrc.server", server)
    win.setProperty("bp.netsrc.port", port)
    win.setProperty("bp.netsrc.path", sub)
    win.setProperty("bp.netsrc.user", user)
    win.setProperty("bp.netsrc.pass", passwd)
    win.setProperty("bp.netsrc.pass.mask", "••••••" if passwd else "")
    win.setProperty("bp.netsrc.write",
                    ("1" if e.get("writeaccess") else "") if e else "")
    # LAST: a read-only scheme must never show the write toggle as on.
    _netsrc_set_ro(win, scheme)
    win.setProperty("bp.netsrc.test", "")
    win.setProperty("bp.netsrc", "open")
    focus_control(792)
    log("netsource: editor open%s" % (" (edit %d)" % edit if edit else ""))


def netproto_next():
    """Cycle the editor protocol through the native schemes (OK on row 793)."""
    win = xbmcgui.Window(10000)
    cur = (win.getProperty("bp.netsrc.scheme") or "ftp://").rstrip(":/").lower()
    try:
        i = NET_PROTOCOLS.index(cur)
    except ValueError:
        i = 0
    nxt = NET_PROTOCOLS[(i + 1) % len(NET_PROTOCOLS)]
    win.setProperty("bp.netsrc.scheme", nxt + "://")
    win.setProperty("bp.netsrc.proto", nxt.upper())
    _netsrc_set_ro(win, nxt)
    log("netsource: protocol %s" % nxt)


def netsrcfield(field):
    """Open the on-screen keyboard for one editor field."""
    mode = {"name": "netname", "server": "netserver", "port": "netport",
            "path": "netpath", "user": "netuser", "pass": "netpass"}.get((field or "").strip())
    if mode:
        xbmc.executebuiltin(
            "RunScript(special://skin/scripts/keyboard.py,open,%s)" % mode)


def netsrcadd():
    """Editor OK: store the entry as-is (label = Name, else a 7-char code; path
    may be empty); no blocking validation. Focus lands on the new row."""
    win = xbmcgui.Window(10000)
    name = (win.getProperty("bp.netsrc.name") or "").strip()
    scheme = (win.getProperty("bp.netsrc.scheme") or "ftp://").strip()
    # Sanitize server/port: spaces can never be part of them.
    server = sources.netsrc_sanitize("server", win.getProperty("bp.netsrc.server"))
    port = sources.netsrc_sanitize("port", win.getProperty("bp.netsrc.port")).strip()
    raw_path = (win.getProperty("bp.netsrc.path") or "").strip()
    user = (win.getProperty("bp.netsrc.user") or "").strip()
    passwd = win.getProperty("bp.netsrc.pass") or ""
    # Store editor values verbatim (entry["fields"]): the assembled path alone
    # cannot round-trip everything.
    fields = {"scheme": scheme.split("://", 1)[0].lower() or "ftp",
              "server": server, "port": port, "path": raw_path,
              "user": user, "pass": passwd}
    url = sources.netsrc_url(scheme, server, port, raw_path, user, passwd)
    if name:
        label = name
    else:
        # language-independent 7-char code, unique among stored labels
        used = {e["label"] for e in sources.netsrc_load()}
        label = ""
        for _ in range(50):
            cand = os.urandom(4).hex()[:7]
            if cand not in used:
                label = cand
                break
        if not label:
            label = os.urandom(4).hex()[:7]
    edit = (win.getProperty("bp.netsrc.edit") or "").strip()
    writeaccess = win.getProperty("bp.netsrc.write") == "1"
    if ((scheme or "").split("://", 1)[0].strip().lower() + "://") in sources.RO_SCHEMES:
        writeaccess = False  # read-only schemes never carry write access
    slot = 0
    if edit.isdigit() and int(edit) >= 1:
        added = sources.netsrc_replace(int(edit), label, url, fields, writeaccess)
        slot = int(edit) if added else 0
    else:
        added = sources.netsrc_add(label, url, fields, writeaccess)
        slot = len(sources.netsrc_load()) if added else 0
    _netsrc_clear_editor()
    netsrc_open()
    drivelist()
    # Re-assert focus until it sticks: the closing modal reassigns it.
    target = (320 + slot) if (added and 1 <= slot <= sources.NETSRC_ROWS) else 273
    for _ in range(8):
        time.sleep(0.15)
        xbmc.executebuiltin("SetFocus(%d)" % target)
        if xbmc.getCondVisibility("Control.HasFocus(%d)" % target):
            break
    log("netsource: OK '%s' (%s)" % (label, redact(url)))


def netsrctest():
    """Test the entered network source via Kodi's VFS (does not add it)."""
    win = xbmcgui.Window(10000)
    scheme = (win.getProperty("bp.netsrc.scheme") or "ftp://").strip()
    server = sources.netsrc_sanitize("server", win.getProperty("bp.netsrc.server"))
    port = sources.netsrc_sanitize("port", win.getProperty("bp.netsrc.port")).strip()
    raw_path = (win.getProperty("bp.netsrc.path") or "").strip()
    user = (win.getProperty("bp.netsrc.user") or "").strip()
    passwd = win.getProperty("bp.netsrc.pass") or ""
    # Never move focus; same assembly as OK (sources.netsrc_url) so Test probes what
    # OK saves. Empty/malformed target simply fails.
    url = sources.netsrc_url(scheme, server, port, raw_path, user, passwd) if server else ""
    ok = False
    missing = _missing_vfs_addon(scheme)
    if missing:
        # The scheme's VFS add-on is missing/disabled: the VFS probe cannot
        # connect, so name the add-on instead of a generic failure.
        pass
    elif url:
        try:
            import xbmcvfs
            res = xbmcvfs.listdir(sources.vfs_dir(url))
            ok = isinstance(res, tuple) and len(res) == 2 and res[0] is not False
            if ok and not res[0] and not res[1]:
                # Kodi's VFS returns empty lists (not False) for a dead host,
                # so an empty result is only valid when the path exists.
                ok = bool(xbmcvfs.exists(url))
        except Exception:
            ok = False
    win.setProperty("bp.netsrc.test", "ok" if ok else "fail")
    if missing:
        _netsrc_notify(31545, error=True, value=missing)
    else:
        _netsrc_notify(31463 if ok else 31464, error=not ok)
    log("netsource: test %s -> %s" % (redact(url) if url else "(no address)",
                                      missing or ("ok" if ok else "fail")))


def netsrcclose():
    """Close the network-source editor modal: focus back to the edited source's
    row, or the + button when adding."""
    win = xbmcgui.Window(10000)
    try:
        edit = int(win.getProperty("bp.netsrc.edit") or 0)
    except (TypeError, ValueError):
        edit = 0
    _netsrc_clear_editor()
    target = (320 + edit) if 1 <= edit <= sources.NETSRC_ROWS else 273
    # Re-assert focus until it sticks: the closing modal reassigns it.
    for _ in range(8):
        time.sleep(0.15)
        xbmc.executebuiltin("SetFocus(%d)" % target)
        if xbmc.getCondVisibility("Control.HasFocus(%d)" % target):
            break
    log("netsource: editor closed (focus %d)" % target)


def netsrc_remove(idx):
    """Remove the 1-based network source and re-render."""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return
    if sources.netsrc_remove(idx):
        netsrc_open()
        drivelist()


def netsrc_toggle(idx):
    """Flip the drive visibility (hidden.drive.<md5(path)>) of the 1-based
    network source; the menu stays open."""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return
    entries = sources.netsrc_load()
    if not 1 <= idx <= len(entries):
        return
    key = sources.netsrc_key(entries[idx - 1], idx)
    try:
        if xbmc.getCondVisibility("Skin.HasSetting(hidden.drive.%s)" % key):
            xbmc.executebuiltin("Skin.Reset(hidden.drive.%s)" % key)
        else:
            xbmc.executebuiltin("Skin.SetBool(hidden.drive.%s)" % key)
    except Exception:
        return
    netsrc_open()
    drivelist()
    log("netsource: toggled slot %d" % idx)


REMOTE_STR = {
    "back": 31376, "home": 31377, "menu": 31378, "up": 31379,
    "down": 31380, "left": 31381, "right": 31382, "ok": 31383,
    "playpause": 31507, "stop": 31508, "rewind": 31386,
    "forward": 31387, "prev": 31388, "next": 31389, "vol_up": 31390,
    "vol_down": 31391, "mute": 31512, "power": 31393, "settings": 31398,
}
REMOTE_ROWS = 3  # user codes shown per function in the Remote tab
REMOTE_DEF_ROWS = 4  # default-code rows per function (max: Back has 4)


def _remote_name(fn):
    """Localized display name of a Remote function id (fallback: English)."""
    sid = REMOTE_STR.get(fn, 0)
    name = xbmc.getLocalizedString(sid) if sid else ""
    if not name:
        for row in _remotes().function_rows():
            if row[0] == fn:
                name = row[1]
                break
    return name or fn


def remote_open():
    """Render the Remote tab: names, default codes and user codes."""
    win = xbmcgui.Window(10000)
    rows = _remotes().function_rows()
    for i, (fn, name, dkeys, ukeys) in enumerate(rows, 1):
        win.setProperty("bp.rem.%d.fn" % i, fn)
        win.setProperty("bp.rem.%d.name" % i,
                        xbmc.getLocalizedString(REMOTE_STR.get(fn, 0)) or name)
        win.clearProperty("bp.rem.%d.def" % i)
        for s in range(REMOTE_DEF_ROWS):
            key = dkeys[s] if s < len(dkeys) else ""
            if key:
                win.setProperty("bp.rem.%d.def%d" % (i, s + 1), key)
                if _remotes().key_on(fn, key):
                    win.setProperty("bp.rem.%d.def%d.on" % (i, s + 1), "1")
                else:
                    win.clearProperty("bp.rem.%d.def%d.on" % (i, s + 1))
            else:
                win.clearProperty("bp.rem.%d.def%d" % (i, s + 1))
                win.clearProperty("bp.rem.%d.def%d.on" % (i, s + 1))
        for s in range(REMOTE_ROWS):
            key = ukeys[s] if s < len(ukeys) else ""
            if key:
                win.setProperty("bp.rem.%d.%d" % (i, s + 1), key)
                if _remotes().key_on(fn, key):
                    win.setProperty("bp.rem.%d.%d.on" % (i, s + 1), "1")
                else:
                    win.clearProperty("bp.rem.%d.%d.on" % (i, s + 1))
            else:
                win.clearProperty("bp.rem.%d.%d" % (i, s + 1))
                win.clearProperty("bp.rem.%d.%d.on" % (i, s + 1))
    log("remotes: %d functions rendered" % len(rows))
    block_render()


def block_render():
    """Render the block-list rubric: spare app, mouse and scanned user keys."""
    win = xbmcgui.Window(10000)
    apps = [r for r in _remotes().block_rows() if r[1] == "app"]
    for i in range(1, 7):
        if i <= len(apps):
            key, _kind, on = apps[i - 1]
            win.setProperty("bp.block.app.%d.key" % i, key)
            win.setProperty("bp.block.app.%d.label" % i, key)
            if on:
                win.setProperty("bp.block.app.%d.on" % i, "1")
            else:
                win.clearProperty("bp.block.app.%d.on" % i)
        else:
            for suffix in ("key", "label", "on"):
                win.clearProperty("bp.block.app.%d.%s" % (i, suffix))
    mice = [r for r in _remotes().block_rows() if r[1] == "mouse"]
    for i in range(1, 4):
        if i <= len(mice):
            key, _kind, on = mice[i - 1]
            win.setProperty("bp.block.%d.key" % i, key)
            win.setProperty("bp.block.%d.label" % i, key)
            if on:
                win.setProperty("bp.block.%d.on" % i, "1")
            else:
                win.clearProperty("bp.block.%d.on" % i)
        else:
            for suffix in ("key", "label", "on"):
                win.clearProperty("bp.block.%d.%s" % (i, suffix))
    users = [r for r in _remotes().block_rows() if r[1] == "user"]
    for i in range(1, 7):
        if i <= len(users):
            key, _kind, on = users[i - 1]
            win.setProperty("bp.block.us.%d.key" % i, key)
            win.setProperty("bp.block.us.%d.label" % i, key)
            if on:
                win.setProperty("bp.block.us.%d.on" % i, "1")
            else:
                win.clearProperty("bp.block.us.%d.on" % i)
        else:
            for suffix in ("key", "label", "on"):
                win.clearProperty("bp.block.us.%d.%s" % (i, suffix))


def blocktoggle(idx):
    """Toggle a mouse-button block row, rebuild the keymap, re-render."""
    win = xbmcgui.Window(10000)
    key = (win.getProperty("bp.block.%s.key" % idx) or "").strip()
    if not key:
        return
    on = _remotes().toggle_block(key)
    if _remotes().build():
        _kmaps()
    xbmc.executebuiltin("ReloadKeymaps")
    block_render()
    log("block: %s -> %s" % (key, "on" if on else "off"))


def blocktoggleapp(idx):
    """Toggle a default spare-button (app/colored) block row, rebuild keymap."""
    win = xbmcgui.Window(10000)
    key = (win.getProperty("bp.block.app.%s.key" % idx) or "").strip()
    if not key:
        return
    on = _remotes().toggle_block(key)
    if _remotes().build():
        _kmaps()
    xbmc.executebuiltin("ReloadKeymaps")
    block_render()
    log("block: %s -> %s" % (key, "on" if on else "off"))


def blockscanopen():
    """Open the key scanner for the block list (adds to block, not a function)."""
    win = xbmcgui.Window(10000)
    win.setProperty("bp.rscan.mode", "block")
    win.setProperty("bp.rscan.fn", "")
    win.setProperty("bp.rscan.start", xbmc.getLocalizedString(31396))
    win.setProperty("bp.rscan.display", xbmc.getLocalizedString(31415))
    win.clearProperty("bp.rscan.code")
    win.clearProperty("bp.rscan.scanning")
    win.clearProperty("bp.scan.armed")
    win.setProperty("bp.rscan", "open")
    focus_control(460)
    log("blockscan: open")


def _debug_logging_on():
    """True when debug logging is enabled (guisettings read; no GetBool flag)."""
    try:
        import xml.etree.ElementTree as ET
        p = xbmcvfs.translatePath("special://userdata/guisettings.xml")
        root = ET.parse(p).getroot()
        for s in root.iter("setting"):
            if s.get("id") == "debug.showloginfo":
                return (s.text or "").strip().lower() == "true"
    except Exception:
        pass
    return False


def _scan_stop():
    """Stop a running key scan and restore debug logging when we enabled it."""
    win = xbmcgui.Window(10000)
    win.clearProperty("bp.scan.armed")
    win.clearProperty("bp.scan.fn")
    if win.getProperty("bp.scan.debugon") == "1":
        try:
            xbmc.executebuiltin("ToggleDebug")
        except Exception:
            pass
        win.clearProperty("bp.scan.debugon")


def remscan_open(idx):
    """Open the key-scanner overlay (bp.rscan) for Remote function `idx` (1-based)."""
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return
    rows = _remotes().function_rows()
    if not (1 <= i <= len(rows)):
        return
    fn = rows[i - 1][0]
    win = xbmcgui.Window(10000)
    win.clearProperty("bp.rscan.mode")
    win.setProperty("bp.rscan.idx", str(i))
    win.setProperty("bp.rscan.fn", fn)
    win.setProperty("bp.rscan.start", xbmc.getLocalizedString(31396))
    win.setProperty("bp.rscan.display", _remote_name(fn))
    win.clearProperty("bp.rscan.code")
    win.clearProperty("bp.rscan.scanning")
    win.clearProperty("bp.scan.armed")
    win.setProperty("bp.rscan", "open")
    focus_control(460)
    log("remscan: open for %s" % fn)


def remscan_start():
    """Start the scan: show 'Scanning...' for up to 3s; needs debug logging
    (HandleKey is debug level), toggled on temporarily when off."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.rscan") != "open":
        return
    mode = win.getProperty("bp.rscan.mode") or ""
    fn = win.getProperty("bp.rscan.fn") or ""
    if not fn and mode != "block":
        return
    win.setProperty("bp.rscan.scanning", "1")
    win.setProperty("bp.rscan.start", xbmc.getLocalizedString(31395))
    if mode == "block":
        win.setProperty("bp.rscan.display", xbmc.getLocalizedString(31415))
    else:
        win.setProperty("bp.rscan.display", _remote_name(fn))
    win.clearProperty("bp.rscan.code")
    win.setProperty("bp.scan.fn", fn)
    win.setProperty("bp.scan.since", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
    win.setProperty("bp.scan.t", str(time.time()))
    if not _debug_logging_on():
        try:
            xbmc.executebuiltin("ToggleDebug")
            win.setProperty("bp.scan.debugon", "1")
        except Exception:
            pass
    win.setProperty("bp.scan.armed", "1")
    log("remscan: scanning for %s" % fn)


def remscan_save():
    """Save the captured code (to a function or the block list) and rebuild keymap."""
    win = xbmcgui.Window(10000)
    mode = win.getProperty("bp.rscan.mode") or ""
    fn = win.getProperty("bp.rscan.fn") or ""
    code = (win.getProperty("bp.rscan.code") or "").strip()
    added = False
    added_fn = ""
    if mode == "block" and code:
        ok, err = _remotes().add_block(code)
        if ok:
            added = True
            if _remotes().build():
                _kmaps()
            xbmc.executebuiltin("ReloadKeymaps")
            block_render()
            log("blockscan: blocked '%s'" % code)
        else:
            log("blockscan: save rejected: %s" % err)
    elif fn and code:
        ok, err = _remotes().add_key(fn, code)
        if ok:
            added_fn = fn
            if _remotes().build():
                _kmaps()
            xbmc.executebuiltin("ReloadKeymaps")
            log("remscan: saved '%s' -> %s" % (code, fn))
        else:
            log("remscan: save rejected: %s" % err)
    was_block = mode == "block"
    _scan_stop()
    _remscan_close()
    remote_open()
    if was_block and added:
        users = [r for r in _remotes().block_rows() if r[1] == "user"]
        slot = next((i + 1 for i, r in enumerate(users) if r[0] == code), len(users))
        time.sleep(0.25)
        xbmc.executebuiltin("SetFocus(%d)" % (544 + max(0, min(slot, 6) - 1)))
    elif added_fn:
        rows = _remotes().function_rows()
        fnidx = next((i + 1 for i, r in enumerate(rows) if r[0] == added_fn), 0)
        ukeys = next((r[3] for r in rows if r[0] == added_fn), [])
        slot = next((i + 1 for i, k in enumerate(ukeys) if k == code), len(ukeys))
        if fnidx and 1 <= slot <= REMOTE_ROWS:
            time.sleep(0.25)
            xbmc.executebuiltin("SetFocus(%d)" % (520 + (fnidx - 1) * REMOTE_ROWS + (slot - 1)))


def remscan_cancel():
    """Close the scanner overlay without saving."""
    _scan_stop()
    _remscan_close()


def _remscan_close():
    win = xbmcgui.Window(10000)
    mode = win.getProperty("bp.rscan.mode") or ""
    try:
        idx = int(win.getProperty("bp.rscan.idx") or 0)
    except (TypeError, ValueError):
        idx = 0
    for prop in ("bp.rscan", "bp.rscan.idx", "bp.rscan.fn", "bp.rscan.code",
                 "bp.rscan.display", "bp.rscan.scanning", "bp.rscan.start",
                 "bp.rscan.mode"):
        win.clearProperty(prop)
    time.sleep(0.2)
    if mode == "block":
        xbmc.executebuiltin("SetFocus(577)")
    elif idx:
        xbmc.executebuiltin("SetFocus(%d)" % (500 + (idx - 1)))


def _remote_key_at(fn, slot):
    """User key at display slot `slot` (1-based) of function `fn`, or ''."""
    for row in _remotes().function_rows():
        if row[0] == fn and 1 <= slot <= len(row[3]):
            return row[3][slot - 1]
    return ""


def _toggle_remote_key(fn, slot):
    """Enable/disable a user key (row toggle), rebuild keymap + re-render."""
    key = _remote_key_at(fn, slot)
    if not key:
        return
    state = _remotes().toggle_key(fn, key)
    if _remotes().build():
        _kmaps()
    xbmc.executebuiltin("ReloadKeymaps")
    remote_open()
    log("remotes: %s %s" % ("on" if state else "off", key))


def _remove_remote_key(fn, slot):
    """Remove a user key, rebuild the keymap + re-render."""
    key = _remote_key_at(fn, slot)
    if key and _remotes().remove_key(fn, key):
        if _remotes().build():
            _kmaps()
        xbmc.executebuiltin("ReloadKeymaps")
        remote_open()
        log("remotes: removed key '%s' from %s" % (key, fn))


def _toggle_default_key(idx, slot):
    """Enable/disable a default key (row toggle, no row menu)."""
    try:
        i, s = int(idx), int(slot)
    except (TypeError, ValueError):
        return
    rows = _remotes().function_rows()
    if not (1 <= i <= len(rows)) or not (1 <= s <= REMOTE_DEF_ROWS):
        return
    fn, _, dkeys, _ = rows[i - 1]
    if s > len(dkeys):
        return
    state = _remotes().toggle_key(fn, dkeys[s - 1])
    if _remotes().build():
        _kmaps()
    xbmc.executebuiltin("ReloadKeymaps")
    remote_open()
    log("remotes: default %s %s" % ("on" if state else "off",
                                   dkeys[s - 1]))


def _rowmenu_state():
    """(kind, idx, focus) from the overlay properties, validated."""
    win = xbmcgui.Window(10000)
    kind = win.getProperty("bp.rowmenu.kind") or ""
    try:
        idx = int(win.getProperty("bp.rowmenu.idx") or 0)
    except (TypeError, ValueError):
        idx = 0
    focus = win.getProperty("bp.rowmenu.focus") or ""
    if kind == "dirsrc":
        label = win.getProperty("bp.dirsrc.%d" % idx)
        ok = 1 <= idx <= 10 and bool(label)
        return (kind, idx, focus, ok)
    if kind == "netsrc":
        label = win.getProperty("bp.net.%d" % idx)
        ok = 1 <= idx <= sources.NETSRC_ROWS and bool(label)
        return (kind, idx, focus, ok)
    if kind == "blacklist":
        label = win.getProperty("bp.bl.%d" % idx)
        ok = 1 <= idx <= 30 and bool(label)
        return (kind, idx, focus, ok)
    if kind == "remotecode":
        fn = win.getProperty("bp.rowmenu.fn") or ""
        return (kind, idx, focus, bool(fn and _remote_key_at(fn, idx)))
    if kind == "block":
        key = win.getProperty("bp.block.us.%d.key" % idx)
        return (kind, idx, focus, bool(key))
    return ("", 0, focus, False)


def rowmenu_open(kind, a, focus, c=""):
    """Open the row menu overlay for a row (toggle + remove)."""
    win = xbmcgui.Window(10000)
    if kind == "remotecode":
        try:
            i, slot = int(a), int(focus)
        except (TypeError, ValueError):
            return
        rows = _remotes().function_rows()
        if not (1 <= i <= len(rows)):
            return
        fn = rows[i - 1][0]
        focus_id = c
        win.setProperty("bp.rowmenu.fn", fn)
        win.setProperty("bp.rowmenu.slot", str(slot))
        win.setProperty("bp.rowmenu.idx", str(slot))
        win.setProperty("bp.rowmenu.fnidx", str(i))
    else:
        try:
            idx = int(a)
        except (TypeError, ValueError):
            return
        focus_id = focus
        win.setProperty("bp.rowmenu.idx", str(idx))
    win.setProperty("bp.rowmenu.kind", kind)
    kind, idx, _, ok = _rowmenu_state()
    if not ok:
        for p in ("bp.rowmenu.kind", "bp.rowmenu.idx", "bp.rowmenu.fn", "bp.rowmenu.slot"):
            win.clearProperty(p)
        return
    try:
        focus_id = str(int(focus_id))
    except (TypeError, ValueError):
        focus_id = ""
    if kind == "blacklist" and win.getProperty("bp.blstd.%d" % idx):
        win.clearProperty("bp.rowmenu.candelete")
    else:
        win.setProperty("bp.rowmenu.candelete", "1")
    # Edit slot: network sources always, blacklist user patterns only.
    if kind == "netsrc" or (kind == "blacklist" and not win.getProperty("bp.blstd.%d" % idx)):
        win.setProperty("bp.rowmenu.canedit", "1")
    else:
        win.clearProperty("bp.rowmenu.canedit")
    if kind == "blacklist":
        title = win.getProperty("bp.bl.%d" % idx) or ""
    elif kind == "dirsrc":
        title = win.getProperty("bp.dirsrc.%d" % idx) or ""
    elif kind == "netsrc":
        title = win.getProperty("bp.net.%d" % idx) or ""
    elif kind == "block":
        title = win.getProperty("bp.block.us.%d.key" % idx) or ""
    else:
        title = _remote_key_at(win.getProperty("bp.rowmenu.fn") or "", idx)
    title = safe_label(title)
    win.setProperty("bp.rowmenu.title", title)
    win.setProperty("bp.rowmenu.title.rep", safe_label(_fticker(title, 11, 380)))
    win.setProperty("bp.rowmenu.focus", focus_id)
    win.setProperty("bp.rowmenu", "open")
    focus_control(447)
    log("rowmenu: open %s %d" % (kind, idx))


def rowmenu_toggle():
    """Toggle the row menu entry; the overlay stays open (live states)."""
    win = xbmcgui.Window(10000)
    kind, idx, _, ok = _rowmenu_state()
    if not ok:
        return
    if kind == "remotecode":
        _toggle_remote_key(win.getProperty("bp.rowmenu.fn") or "", idx)
    elif kind == "blacklist":
        blacklist_toggle(idx)
    elif kind == "netsrc":
        netsrc_toggle(idx)
    elif kind == "block":
        key = win.getProperty("bp.block.us.%d.key" % idx) or ""
        if key:
            _remotes().toggle_block(key)
            if _remotes().build():
                _kmaps()
            xbmc.executebuiltin("ReloadKeymaps")
            block_render()
    else:
        dirsrc_toggle(idx)


def rowmenu_remove():
    """Remove the row menu entry, then close onto the add button."""
    win = xbmcgui.Window(10000)
    kind, idx, _, ok = _rowmenu_state()
    if not ok:
        return
    if kind == "remotecode":
        _remove_remote_key(win.getProperty("bp.rowmenu.fn") or "", idx)
    elif kind == "blacklist":
        blacklist_remove(idx)
    elif kind == "netsrc":
        netsrc_remove(idx)
    elif kind == "block":
        key = win.getProperty("bp.block.us.%d.key" % idx) or ""
        if key:
            _remotes().remove_block(key)
            if _remotes().build():
                _kmaps()
            xbmc.executebuiltin("ReloadKeymaps")
            block_render()
    else:
        dirsrc_remove(idx)
    try:
        fnidx = int(win.getProperty("bp.rowmenu.fnidx") or 0)
    except (TypeError, ValueError):
        fnidx = 0
    win.clearProperty("bp.rowmenu")
    win.clearProperty("bp.rowmenu.kind")
    win.clearProperty("bp.rowmenu.idx")
    win.clearProperty("bp.rowmenu.fn")
    win.clearProperty("bp.rowmenu.slot")
    win.clearProperty("bp.rowmenu.fnidx")
    win.clearProperty("bp.rowmenu.candelete")
    win.clearProperty("bp.rowmenu.canedit")
    win.clearProperty("bp.rowmenu.focus")
    win.clearProperty("bp.rowmenu.title")
    win.clearProperty("bp.rowmenu.title.rep")
    time.sleep(0.3)
    if kind == "remotecode":
        xbmc.executebuiltin("SetFocus(%d)" % (500 + max(0, fnidx - 1)))
    elif kind == "blacklist":
        xbmc.executebuiltin("SetFocus(272)")
    elif kind == "netsrc":
        xbmc.executebuiltin("SetFocus(273)")
    elif kind == "block":
        xbmc.executebuiltin("SetFocus(577)")
    else:
        xbmc.executebuiltin("SetFocus(271)")
    log("rowmenu: removed %s %d" % (kind, idx))


def rowmenu_close():
    """Close the row menu overlay, focus back to the opener row."""
    win = xbmcgui.Window(10000)
    kind, _, focus, _ = _rowmenu_state()
    try:
        fnidx = int(win.getProperty("bp.rowmenu.fnidx") or 0)
    except (TypeError, ValueError):
        fnidx = 0
    win.clearProperty("bp.rowmenu")
    win.clearProperty("bp.rowmenu.kind")
    win.clearProperty("bp.rowmenu.idx")
    win.clearProperty("bp.rowmenu.fn")
    win.clearProperty("bp.rowmenu.slot")
    win.clearProperty("bp.rowmenu.fnidx")
    win.clearProperty("bp.rowmenu.candelete")
    win.clearProperty("bp.rowmenu.canedit")
    win.clearProperty("bp.rowmenu.focus")
    win.clearProperty("bp.rowmenu.title")
    win.clearProperty("bp.rowmenu.title.rep")
    if not focus:
        if kind == "remotecode":
            focus = str(500 + max(0, fnidx - 1))
        elif kind == "blacklist":
            focus = "272"
        else:
            focus = "271"
    time.sleep(0.25)
    xbmc.executebuiltin("SetFocus(%s)" % focus)


def rowmenu_edit():
    """Row menu EDIT: open the editor pre-filled (netsrcnew / blacklistedit);
    baseline blacklist values are never editable."""
    win = xbmcgui.Window(10000)
    kind, idx, _, ok = _rowmenu_state()
    if not ok:
        return
    props = ("bp.rowmenu", "bp.rowmenu.kind", "bp.rowmenu.idx",
             "bp.rowmenu.fn", "bp.rowmenu.slot", "bp.rowmenu.fnidx",
             "bp.rowmenu.candelete", "bp.rowmenu.canedit",
             "bp.rowmenu.focus", "bp.rowmenu.title", "bp.rowmenu.title.rep")
    if kind == "blacklist":
        pattern = (win.getProperty("bp.bl.%d" % idx) or "").strip()
        if not pattern or win.getProperty("bp.blstd.%d" % idx):
            return
        for p in props:
            win.clearProperty(p)
        win.setProperty("bp.blacklist.edit", str(idx))
        xbmc.executebuiltin(
            "RunScript(special://skin/scripts/keyboard.py,open,blacklistedit)")
        log("rowmenu: edit blacklist %d" % idx)
        return
    if kind != "netsrc":
        return
    for p in props:
        win.clearProperty(p)
    netsrcnew(idx)


def dirsrc_toggle(idx):
    """Toggle directory-source visibility by PATH hash (hide.dirsource.<md5>),
    so it stays hidden across removals/unplugs."""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return
    lst = _dirsources().load()
    if not 1 <= idx <= len(lst):
        return
    key = sources.dirsource_key(lst[idx - 1])
    xbmc.executebuiltin("Skin.ToggleSetting(hide.dirsource.%s)" % key)
    dirsrc_open()
    drivelist()


def scan_arm(clear):
    """Arm/re-arm the key-code scan pill: stamp now so the daemon shows the
    next HandleKey line as bp.scan.code (needs debug logging)."""
    win = xbmcgui.Window(10000)
    if clear:
        win.clearProperty("bp.scan.code")
    win.setProperty("bp.scan.since", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
    win.setProperty("bp.scan.t", str(time.time()))
    win.setProperty("bp.scan.armed", "1")



def picker_open():
    """Open the folder picker from the current source root, keeping the browsed
    folder as return path; a reload is required (stale context menus)."""
    win = xbmcgui.Window(10000)
    cur = path_dec(win.getProperty("bp.path") or "").strip()
    if xbmc.getCondVisibility("Player.HasAudio"):
        xbmc.Player().stop()
        log("picker: stopped playback (footer overlaps the picker bar)")
    src = sources.rstrip_slash(path_dec(win.getProperty("bp.src") or ""))
    start = src if src and os.path.isdir(src) else cur
    try:
        dir_roots = {sources.rstrip_slash(p or "") for p in _dirsources().load()}
    except Exception:
        dir_roots = set()
    if (not (start and os.path.isdir(start))
            or sources.is_network_path(src) or src in dir_roots):
        # Directory/network context is not a valid pick target (hidden from the
        # picker dropdown) -> jump to the first local drive root.
        first = ""
        try:
            for d in sources.visible(sources.all_drives()):
                if (d.get("type") or "") in ("dirsource", "network"):
                    continue
                p = sources.rstrip_slash(d.get("path") or "")
                if p and os.path.isdir(p):
                    first = p
                    break
        except Exception:
            first = ""
        if first:
            start = first
            win.setProperty("bp.src", path_enc(first))
            win.setProperty("bp.title", sources.short_label(first))
            try:
                win.setProperty("bp.current.icon", sources.icon_for(first))
            except Exception:
                pass
            reset_top()
            log("picker: network context -> first local drive %s" % first)
    win.setProperty("bp.pick.return", path_enc(cur))
    win.setProperty("bp.pick.src", win.getProperty("bp.src") or "")
    win.setProperty("bp.pick.active", "1")
    # Close the settings dialog (1150): the picker is a Home-window construct,
    # a modal would sit on top. Its onunload drops bp.return.focus -> clear it.
    try:
        if xbmc.getCondVisibility("Window.IsActive(1150)"):
            xbmc.executebuiltin("Dialog.Close(1150)")
    except Exception:
        pass
    win.clearProperty("bp.settings")
    win.clearProperty("bp.menu")
    win.clearProperty("bp.drives")
    win.clearProperty("bp.return.focus")
    if start.rstrip("/") != cur.rstrip("/"):
        set_current(start)
    # Clear a leftover search (else the picker list becomes recursive matches).
    win.clearProperty("search.query")
    _bump_list()
    drivelist()
    # Home's onload may fight for focus after the window switch; re-assert.
    for _ in range(8):
        xbmc.executebuiltin("SetFocus(33)")
        try:
            if xbmc.getCondVisibility("Control.HasFocus(33)"):
                break
        except Exception:
            break
        time.sleep(0.15)
    log("picker open: start %s, return %s" % (start, cur))


def picker_cancel():
    """Cancel picker and stay at the root of the currently shown source."""
    win = xbmcgui.Window(10000)
    cur = path_dec(win.getProperty("bp.path") or "").strip()
    win.clearProperty("bp.pick.active")
    win.clearProperty("bp.pick.return")
    win.clearProperty("bp.pick.src")
    win.clearProperty("bp.return.focus")
    root = current_source_root(cur) if cur else ""
    if root and (sources.is_network_path(root) or os.path.isdir(root)):
        win.setProperty("bp.src", path_enc(root))
        win.setProperty("bp.title", sources.short_label(root))
        try:
            win.setProperty("bp.current.icon", sources.icon_for(root))
        except Exception:
            pass
        reset_top()
        set_current(root)
        log("picker cancel, stay at source %s" % root)
    else:
        log("picker cancel")
    drivelist()
    # Force a reload: with no navigation the URL is unchanged but files must
    # become live again.
    _bump_list()
    try:
        xbmc.executebuiltin("SetFocus(33)")
    except Exception:
        pass


def _add_dirsource(cur):
    """Add `cur` as a directory source and land in it; shared by picker_select
    and pickadd."""
    win = xbmcgui.Window(10000)
    cur = (cur or "").strip().rstrip("/")

    def _refocus():
        for _ in range(8):
            xbmc.executebuiltin("SetFocus(33)")
            try:
                if xbmc.getCondVisibility("Control.HasFocus(33)"):
                    break
            except Exception:
                break
            time.sleep(0.15)

    def _warn(rid):
        try:
            xbmcgui.Dialog().notification(xbmc.getLocalizedString(31303),
                                          xbmc.getLocalizedString(rid),
                                          xbmcgui.NOTIFICATION_WARNING, 2000)
        except Exception:
            pass

    if not cur or not os.path.isdir(cur):
        _warn(31330)  # file not found
        _refocus()
        return
    # Only subfolders, not source roots
    if cur == current_source_root(cur):
        _warn(31357)
        _refocus()
        return
    if cur in _dirsources().load():
        _warn(31358)
        _refocus()
        return
    added = _dirsources().add(cur)
    # Close the picker BEFORE re-rendering (drivelist maps through the filter).
    ret = path_dec(win.getProperty("bp.pick.return") or "").strip()
    saved_src = path_dec(win.getProperty("bp.pick.src") or "")
    win.clearProperty("bp.pick.active")
    win.clearProperty("bp.pick.return")
    win.clearProperty("bp.pick.src")
    win.clearProperty("bp.return.focus")
    if added:
        dirsrc_open()
        drivelist()
        log("dirsource added '%s'" % redact(cur))
    # On success land in the new source; else fall back to the previous location.
    if added:
        win.setProperty("bp.src", path_enc(cur))
        win.setProperty("bp.title", sources.short_label(cur))
        win.setProperty("bp.current.icon", sources.icon_for(cur))
        set_current(cur)
        log("dirsource -> entered new source %s" % redact(cur))
    elif ret and os.path.isdir(ret):
        if saved_src:
            win.setProperty("bp.src", path_enc(saved_src))
            win.setProperty("bp.title", sources.short_label(saved_src))
            win.setProperty("bp.current.icon", sources.icon_for(saved_src))
        set_current(ret)
        log("dirsource restore %s" % redact(ret))
    else:
        log("dirsource done")
    # Force a reload (same no-navigation case as picker_cancel).
    _bump_list()
    _refocus()


def picker_select():
    """Add the currently browsed folder as directory source (bottom-bar Select)."""
    _add_dirsource(path_dec(xbmcgui.Window(10000).getProperty("bp.path") or ""))


def pickadd(path):
    """Picker context menu: add the chosen folder as a directory source directly."""
    _add_dirsource(path_dec(path or ""))


def srcask(path):
    """Picker context menu: confirm modal before adding the folder as a source."""
    win = xbmcgui.Window(10000)
    p = path_dec(path or "").strip().rstrip("/")
    if not p or not os.path.isdir(p):
        try:
            xbmcgui.Dialog().notification(xbmc.getLocalizedString(31303),
                                          xbmc.getLocalizedString(31330),
                                          xbmcgui.NOTIFICATION_WARNING, 2000)
        except Exception:
            pass
        return
    win.setProperty("bp.srcq.title", safe_label(os.path.basename(p) or p))
    win.setProperty("bp.srcq.line", xbmc.getLocalizedString(31425))
    win.setProperty("bp.srcq.path", path_enc(p))
    win.setProperty("bp.srcq", "open")
    focus_control(921)  # No (safe default)
    log("srcask: %s" % p)


def srcadd():
    """Yes-handler of the srcq confirm modal: add bp.srcq.path as a source."""
    win = xbmcgui.Window(10000)
    p = path_dec(win.getProperty("bp.srcq.path") or "").strip()
    win.clearProperty("bp.srcq")
    win.clearProperty("bp.srcq.path")
    win.clearProperty("bp.srcq.title")
    win.clearProperty("bp.srcq.line")
    _add_dirsource(p)


def power(action):
    """Quit/Restart with a pre-exit handshake: set bp.exit, wait for the daemons
    to exit, then fire. Daemons have no addon context so never see abortRequested."""
    win = xbmcgui.Window(10000)
    win.setProperty("bp.exit", "1")
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if (not win.getProperty("bp.home-daemon.running")
                and not win.getProperty("bp.foldersize.running")):
            break
        xbmc.sleep(100)
    xbmc.executebuiltin(action)
    # Non-terminating actions (Suspend resume) never reload Home, so clear
    # bp.exit on a short AlarmClock or the daemons stay dead.
    try:
        xbmc.executebuiltin(
            "AlarmClock(bp_exitclear,ClearProperty(bp.exit,10000),00:05,silent)")
    except Exception:
        pass


def poweropen():
    """Open the generated shutdown overlay (bp.power) and focus the first row (960)."""
    win = xbmcgui.Window(10000)
    win.clearProperty("bp.menu")
    win.setProperty("bp.power", "open")
    # Re-assert focus until it sticks: on a slow box a single SetFocus does
    # nothing and arrows move the file list behind the menu.
    focus_control(960, tries=8, interval=0.15)
    log("power menu open")


INFO_ROWS = 20
INFO_COVERS = 3  # cover slots above the rows


def _info_size(n):
    try:
        n = float(n)
    except Exception:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%.1f TB" % n


def _info_duration(sec):
    try:
        sec = int(round(float(sec)))
    except Exception:
        return ""
    if sec <= 0:
        return ""
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def _info_fps(v):
    try:
        v = float(v)
    except Exception:
        return ""
    return ("%d" % round(v)) if abs(v - round(v)) < 0.05 else ("%.1f" % v)


def _info_khz(hz):
    try:
        hz = int(hz)
    except Exception:
        return ""
    if hz <= 0:
        return ""
    khz = hz / 1000.0
    return ("%d kHz" % round(khz)) if abs(khz - round(khz)) < 0.05 else ("%.1f kHz" % khz)


def _audio_track_text(track):
    """`lang (name) · codec / channels / kHz` for one audio track dict."""
    tag = " · ".join(x for x in (
        track.get("lang"),
        ("(%s)" % track["name"]) if track.get("name") else "") if x)
    fmt = " / ".join(x for x in (
        track.get("codec"),
        str(track["channels"]) if track.get("channels") else "",
        _info_khz(track.get("sample_rate"))) if x)
    return (tag + " · " + fmt) if (tag and fmt) else (tag or fmt)


def _subtitle_tag(st):
    """Display tag for one player subtitle stream dict; decodes the
    percent-encoded external filename artifact Kodi produces for VFS paths."""
    lang = (st.get("language") or "").strip()
    name = (st.get("name") or "").strip()
    ext = (xbmc.getLocalizedString(21602) or "").strip()  # Kodi "(External)"
    marker = ""
    if ext and name.endswith(ext):
        name = name[:-len(ext)].strip()
        marker = ext
    name = sources.url_unquote(name)
    name = " ".join(safe_label(name).split())
    # Encoding artifact: the digit tail of a trimmed '%NN' separator.
    parts = name.split(" ")
    while len(parts) > 1 and parts[0].isdigit():
        parts.pop(0)
    name = " ".join(parts)
    if marker:
        name = ("%s %s" % (name, marker)).strip()
    if lang and name:
        return "%s · %s" % (lang, name)
    return lang or name


def info_scan():
    """Fill bp.info.1..N.key/value from the playing file (metadata.py, not
    Kodi infolabels); a row shows only while its key is non-empty."""
    win = xbmcgui.Window(10000)
    # bp.info.done is the waiting indicator: cleared here, set on every exit.
    win.clearProperty("bp.info.done")
    for i in range(1, INFO_ROWS + 1):
        win.clearProperty("bp.info.%d.key" % i)
        win.clearProperty("bp.info.%d.value" % i)
        win.clearProperty("bp.info.%d.value.rep" % i)
    for i in range(1, INFO_COVERS + 1):
        win.clearProperty("bp.info.cover.%d" % i)
    win.clearProperty("bp.as.multi")
    win.clearProperty("bp.as.label")
    for i in range(1, INFO_ROWS + 1):
        win.clearProperty("bp.info.%d.track" % i)
        win.clearProperty("bp.info.%d.current" % i)
    try:
        path = xbmc.Player().getPlayingFile()
    except Exception:
        path = ""
    if not path:
        win.setProperty("bp.info.done", "1")
        return
    try:
        import metadata
        # The home daemon prefetches metadata on playback start; a cache miss
        # is read now and remembered.
        m = metadata.load_cache(path)
        if m is None:
            m = metadata.read_info(path)
            metadata.store_cache(path, m)
    except Exception as e:
        # Log real failures: swallowing them made a whole file show only size.
        m = {}
        log("info: metadata failed for %s: %s" % (redact(path), e))
    entries = []

    def add(sid, value):
        value = ("" if value is None else str(value)).strip()
        if value:
            entries.append((sid, value))

    def add_audio(tracks):
        """One row per audio track; numbered keys when more than one track."""
        multi = len(tracks) > 1
        for n, t in enumerate(tracks, start=1):
            key = ("%s %d" % (xbmc.getLocalizedString(31443), n)) if multi else 31443
            add(key, _audio_track_text(t))

    add(31427, m.get("title"))
    add(31428, m.get("artist"))
    add(31429, m.get("album"))
    add(31430, m.get("year"))
    add(31431, m.get("genre"))
    add(31432, _info_duration(m.get("duration")))
    has_video = bool(m.get("vcodec") or m.get("width"))
    if has_video:
        add(31433, m.get("vcodec"))
        if m.get("width") and m.get("height"):
            add(31491, "%d×%d px" % (m["width"], m["height"]))
        add(31444, m.get("hdr"))
        fps = _info_fps(m.get("fps"))
        add(31442, (fps + " fps") if fps else "")
        if m.get("bitrate"):
            add(31435, "%.0f kbps" % m["bitrate"])
        tracks = m.get("audio")
        if not tracks and m.get("acodec"):
            tracks = [{"codec": m.get("acodec"), "channels": m.get("channels"),
                       "sample_rate": m.get("sample_rate")}]
        streams, pos, subs, spos, senabled = _player_all()
        if len(streams) > 1:
            # Source rows from the PLAYER so each carries its stream index.
            ptracks = []
            for st in streams:
                ptracks.append({
                    "codec": st.get("codec"),
                    "channels": st.get("channels"),
                    "sample_rate": st.get("samplerate"),
                    "lang": st.get("language"), "name": st.get("name"),
                    "_index": st.get("index")})
            first = len(entries)
            add_audio(ptracks)
            for k, t in enumerate(ptracks):
                row = first + k + 1
                if row > INFO_ROWS:
                    break
                win.setProperty("bp.info.%d.track" % row,
                                "a:%s" % t["_index"])
                if k == pos:
                    win.setProperty("bp.info.%d.current" % row, "1")
        else:
            add_audio(tracks or [])
        if subs:
            # Subtitle rows are player-sourced (external .srt has no container
            # entry); the trailing Off row selects none.
            sub_base = xbmc.getLocalizedString(31445)
            for k, st in enumerate(subs):
                tag = _subtitle_tag(st)
                entries.append(("%s %d" % (sub_base, k + 1),
                                tag or ("Stream %d" % (k + 1))))
                row = len(entries)
                if row > INFO_ROWS:
                    break
                win.setProperty("bp.info.%d.track" % row,
                                "s:%s" % st.get("index"))
                if senabled and k == spos:
                    win.setProperty("bp.info.%d.current" % row, "1")
            entries.append((sub_base, xbmc.getLocalizedString(31446)))
            row = len(entries)
            if row <= INFO_ROWS:
                win.setProperty("bp.info.%d.track" % row, "s:off")
                if not senabled:
                    win.setProperty("bp.info.%d.current" % row, "1")
    else:
        add(31433, m.get("acodec"))
        if m.get("bitrate"):
            add(31435, "%.0f kbps" % m["bitrate"])
        add(31436, _info_khz(m.get("sample_rate")))
        add(31437, m.get("channels"))
    # Covers: embedded audio pictures or video attachments (MKV cover first).
    try:
        cps = metadata.covers_cached(path)
        if cps is None:
            # Cold cache: only local files are read synchronously; extracting a
            # network container would block the modal.
            cps = [] if sources.is_network_path(path) else metadata.covers(path)
        for i, cp in enumerate(cps[:INFO_COVERS], start=1):
            win.setProperty("bp.info.cover.%d" % i, cp)
    except Exception as e:
        log("info: covers failed for %s: %s" % (redact(path), e))
    try:
        if sources.is_network_path(path):
            # VFS URL: size via xbmcvfs; mtime via sources.net_mtime (PROPFIND).
            add(31440, _info_size(xbmcvfs.File(path).size()))
            mt = sources.net_mtime(path)
            if mt:
                add(31441, time.strftime("%Y-%m-%d %H:%M", time.localtime(mt)))
        else:
            st = os.stat(path)
            add(31440, _info_size(st.st_size))
            add(31441, time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)))
    except Exception:
        pass
    for i, (sid, value) in enumerate(entries[:INFO_ROWS], start=1):
        win.setProperty("bp.info.%d.key" % i,
                        sid if isinstance(sid, str) else xbmc.getLocalizedString(sid))
        win.setProperty("bp.info.%d.value" % i, value)
        # Pre-repeated ticker text: Kodi labels stop after 2 scroll loops, so
        # long values are repeated to cover ~10 min; short values stay static.
        win.setProperty("bp.info.%d.value.rep" % i, _fticker(value, 16, 520))
    win.setProperty("bp.info.done", "1")
    log("info: %d metadata rows (%s)" % (min(len(entries), INFO_ROWS),
                                         "video" if has_video else "audio"))


def _info_clear(win):
    """Clear the INFO modal rows before first show (no flash of the previous
    file); NOT called on the slow-scan re-show."""
    win.clearProperty("bp.info.done")
    for i in range(1, INFO_ROWS + 1):
        win.clearProperty("bp.info.%d.key" % i)
        win.clearProperty("bp.info.%d.value" % i)
        win.clearProperty("bp.info.%d.value.rep" % i)
        win.clearProperty("bp.info.%d.track" % i)
        win.clearProperty("bp.info.%d.current" % i)
    for i in range(1, INFO_COVERS + 1):
        win.clearProperty("bp.info.cover.%d" % i)
    win.clearProperty("bp.as.multi")
    win.clearProperty("bp.as.label")


def _info_show(win):
    """Make the INFO modal visible and park focus so Back/click closes it."""
    win.setProperty("bp.info", "open")
    for _ in range(6):
        xbmc.executebuiltin("SetFocus(520)")
        try:
            if xbmc.getCondVisibility("Control.HasFocus(520)"):
                break
        except Exception:
            break
        time.sleep(0.1)


def infoopen():
    """Open the player INFO modal: show first, then scan; a slow scan re-shows
    because Kodi does not re-evaluate `<visible>` on property changes."""
    win = xbmcgui.Window(10000)
    try:
        frm = xbmc.getInfoLabel("System.CurrentControlId")
    except Exception:
        frm = ""
    if frm and frm.isdigit():
        win.setProperty("bp.info.from", frm)
    _info_clear(win)
    _info_show(win)
    started = time.time()
    info_scan()
    took = time.time() - started
    if win.getProperty("bp.info") != "open":
        log("info modal closed during scan, staying closed")
        return
    if took > 0.25:
        # Slow scan: hide + show once so filled rows render.
        win.clearProperty("bp.info")
        time.sleep(0.1)
        _info_show(win)
        log("info: scan took %.1fs, modal re-shown afterwards" % took)
    if win.getProperty("bp.info.1.key"):
        for _ in range(6):
            xbmc.executebuiltin("SetFocus(710)")
            try:
                if xbmc.getCondVisibility("Control.HasFocus(710)"):
                    break
            except Exception:
                break
            time.sleep(0.1)
    log("info modal open")


def _photo_current_path(win):
    """Original path of the current photo (playlist entry, not the cache path)."""
    try:
        plist = [path_dec(x) for x in json.loads(win.getProperty("bp.photo.list") or "[]")]
        idx = int(win.getProperty("bp.photo.idx") or "0")
        if plist and 0 <= idx < len(plist):
            return plist[idx]
    except Exception:
        pass
    try:
        return path_dec(win.getProperty("bp.photo.path") or "")
    except Exception:
        return ""


def _photo_info_fill(win, path):
    """Fill bp.info.1..N from a photo's EXIF data (photo INFO modal)."""
    win.clearProperty("bp.info.done")
    for i in range(1, INFO_ROWS + 1):
        win.clearProperty("bp.info.%d.key" % i)
        win.clearProperty("bp.info.%d.value" % i)
        win.clearProperty("bp.info.%d.value.rep" % i)
        win.clearProperty("bp.info.%d.track" % i)
        win.clearProperty("bp.info.%d.current" % i)
    for i in range(1, INFO_COVERS + 1):
        win.clearProperty("bp.info.cover.%d" % i)
    entries = []

    def add(sid, value):
        value = ("" if value is None else str(value)).strip()
        if value:
            entries.append((sid, value))

    d = {}
    try:
        from fileops import _exif_data
        d = _exif_data(path)
    except Exception as e:
        log("photo info: exif failed for %s: %s" % (redact(path), e))
    try:
        add(31427, safe_label(os.path.basename(sources.url_unquote(path or ""))))
    except Exception:
        pass
    cam = ((d.get("make") or "").strip() + " " + (d.get("model") or "").strip()).strip()
    if cam:
        add(31485, cam)
    add(31486, d.get("lens"))
    add(31492, d.get("orientation"))
    # EXIF capture date: own entry, in addition to the file date.
    add(31487, d.get("datetime"))
    add(31488, d.get("exposure"))
    add(31489, d.get("fnumber"))
    add(31520, d.get("iso"))
    add(31490, d.get("focal"))
    add(31491, d.get("pixels"))
    # File metadata: size, then mtime (os.stat locally, VFS stat for network).
    try:
        if sources.is_network_path(path):
            size = xbmcvfs.File(path).size()
        else:
            size = os.path.getsize(path)
        add(31440, _info_size(size))
    except Exception:
        pass
    mt = 0
    try:
        if sources.is_network_path(path):
            mt = sources.net_mtime(path)
        else:
            mt = os.path.getmtime(path)
    except Exception:
        mt = 0
    if mt:
        add(31441, time.strftime("%Y-%m-%d %H:%M", time.localtime(mt)))
    for i, (sid, value) in enumerate(entries[:INFO_ROWS], start=1):
        win.setProperty("bp.info.%d.key" % i,
                        sid if isinstance(sid, str) else xbmc.getLocalizedString(sid))
        win.setProperty("bp.info.%d.value" % i, value)
        win.setProperty("bp.info.%d.value.rep" % i, _fticker(value, 16, 520))
    win.setProperty("bp.info.done", "1")


def photoexif():
    """Open the shared INFO modal with the current photo's EXIF data; same
    show-scan-reshow pattern as infoopen."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.photo") != "open":
        return
    path = _photo_current_path(win)
    try:
        frm = xbmc.getInfoLabel("System.CurrentControlId")
    except Exception:
        frm = ""
    if frm and frm.isdigit():
        win.setProperty("bp.info.from", frm)
    _info_clear(win)
    _info_show(win)
    started = time.time()
    _photo_info_fill(win, path)
    took = time.time() - started
    if win.getProperty("bp.info") != "open":
        log("photo exif modal closed during fill, staying closed")
        return
    if took > 0.25:
        # Slow fill: hide + show once so filled rows render.
        win.clearProperty("bp.info")
        time.sleep(0.1)
        _info_show(win)
        log("photo info: fill took %.1fs, modal re-shown afterwards" % took)
    if win.getProperty("bp.info.1.key"):
        for _ in range(6):
            xbmc.executebuiltin("SetFocus(710)")
            try:
                if xbmc.getCondVisibility("Control.HasFocus(710)"):
                    break
            except Exception:
                break
            time.sleep(0.1)
    log("photo exif modal open: %s" % redact(path))


def infoclose():
    """Close the INFO modal and restore focus to the button that opened it."""
    win = xbmcgui.Window(10000)
    win.clearProperty("bp.info")
    frm = win.getProperty("bp.info.from")
    win.clearProperty("bp.info.from")
    if frm and frm.isdigit():
        time.sleep(0.1)
        xbmc.executebuiltin("SetFocus(%s)" % frm)
    log("info modal closed")


def keysopen():
    """Open the keyboard shortcut map (bp.keys); bounded SetFocus retry because
    `<visible>` may not be re-evaluated yet."""
    win = xbmcgui.Window(10000)
    win.setProperty("bp.keys", "open")
    for _ in range(10):
        xbmc.executebuiltin("SetFocus(760)")
        try:
            if xbmc.getCondVisibility("Control.HasFocus(760)"):
                break
        except Exception:
            break
        time.sleep(0.05)
    log("keys modal open")


def openlink():
    """Open the project page in the system browser (About modal link)."""
    url = "https://github.com/cyberpixelsharc/browsybare"
    # Android/TV: Python's webbrowser has no browser command to run, so it fails
    # ("no browser found"). Kodi's Android VIEW intent opens the system browser.
    try:
        if xbmc.getCondVisibility("System.Platform.Android"):
            xbmc.executebuiltin(
                'StartAndroidActivity("","android.intent.action.VIEW","","%s")' % url)
            return
    except Exception:
        pass
    import webbrowser
    try:
        if webbrowser.open(url):
            return
    except Exception:
        pass
    try:
        xbmcgui.Dialog().notification(
            skin_name(),
            xbmc.getLocalizedString(31521),
            xbmcgui.NOTIFICATION_WARNING, 4000)
    except Exception:
        pass


def _version_tuple(text):
    parts = re.findall(r"\d+", text or "")
    return tuple(int(p) for p in parts[:4]) if parts else (0,)


UPDATE_SEARCH_HOLD = 2.0  # keep "Searching for update" visible at least this long
UPDATE_PHASE_HOLD = 2.0  # and each download/save phase label too


def _hold(t0, secs):
    """Keep a transient status label up for at least `secs` seconds so a fast
    step does not make it flash by."""
    try:
        rest = secs - (time.time() - t0)
        if rest > 0:
            time.sleep(rest)
    except Exception:
        pass


def _search_hold(t0):
    _hold(t0, UPDATE_SEARCH_HOLD)


def _dev_update():
    """Dev-only override (state_dir/dev-update.json): {"version": "...",
    "source": "<local zip path or URL>"}. Lets a dev point the updater at a
    local build without a GitHub release; returns (version, source) or None."""
    try:
        d = read_json(state_file("dev-update.json"), None)
        if isinstance(d, dict) and d.get("version") and d.get("source"):
            return str(d["version"]), str(d["source"])
    except Exception:
        pass
    return None


def _notify_update(message):
    """Top-right Kodi notification for an updater failure, including the reason
    (so a device without easy log access still shows what went wrong)."""
    try:
        xbmcgui.Dialog().notification(
            xbmc.getLocalizedString(31535), safe_label(str(message)),
            xbmcgui.NOTIFICATION_ERROR, 10000)
    except Exception:
        pass


def _github_latest():
    """Newest release (version, zip URL, error) from the GitHub API; version is
    None on failure and the error carries the reason."""
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.github.com/repos/cyberpixelsharc/browsybare/releases/latest",
            headers={"User-Agent": "Browsybare"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log("update check failed: %s" % e)
        return None, None, "%s: %s" % (type(e).__name__, e)
    latest = (data.get("tag_name") or "").lstrip("vV").strip()
    url = ""
    for a in (data.get("assets") or []):
        u = a.get("browser_download_url") or ""
        if u.lower().endswith(".zip"):
            url = u
            break
    if latest and not url:
        url = ("https://github.com/cyberpixelsharc/browsybare/releases/"
               "download/v%s/browsybare-%s.zip" % (latest, latest))
    return latest, url, ""


def _set_avail(win, latest, url):
    """Stage an available update: button label + state for the install flow."""
    win.setProperty("bp.update.state", "avail")
    win.setProperty("bp.update.ver", latest)
    win.setProperty("bp.update.url", url)
    _update_text(win, 31531, latest)  # "New version %s available", until clicked


def update_check():
    """Ask GitHub for the newest release tag. If it is newer, turn the button
    into a Download+Install action; otherwise show a 3 s info. Nothing is
    installed automatically. A dev-update.json overrides the GitHub lookup."""
    win = xbmcgui.Window(10000)
    for p in ("bp.update.state", "bp.update.ver", "bp.update.url"):
        win.clearProperty(p)
    _update_text(win, 31540)  # "Searching for update", shown during the request
    t0 = time.time()
    current = win.getProperty("bp.version") or ""
    dev = _dev_update()
    if dev:
        # Dev override: offer the given build directly, no version comparison
        # (install the repo's own zip to exercise the update path safely).
        latest, url = dev
        log("update: dev override -> %s" % latest)
        _search_hold(t0)
        _set_avail(win, latest, url)
        return
    latest, url, err = _github_latest()
    if latest is None:
        _search_hold(t0)
        _notify_update("%s: %s" % (xbmc.getLocalizedString(31526), err))
        _update_result(win, 31526)
        return
    _search_hold(t0)
    if latest and _version_tuple(latest) > _version_tuple(current):
        _set_avail(win, latest, url)
    else:
        _update_result(win, 31524)


def update_autocheck():
    """Opt-in automatic update check (setting update.autocheck), one per session,
    run from its own script (boot triggers it) so the network wait never blocks
    the UI. Silent unless a newer release exists, then it opens the same install
    confirmation as the manual check. Declining is not repeated until the next
    session (the boot trigger is one-shot)."""
    log("update autocheck: start")
    mon = xbmc.Monitor()
    if mon.waitForAbort(10):  # let the skin settle after boot
        return
    try:
        if not xbmc.getCondVisibility("Skin.HasSetting(update.autocheck)"):
            log("update autocheck: disabled")
            return
    except Exception:
        return
    win = xbmcgui.Window(10000)
    # The boot trigger is one-shot, so WAIT (bounded, ~60 s) for a quiet Home
    # instead of silently wasting the session's only check when a player or an
    # overlay is up at the 10 s mark.
    reason = "unknown"
    for _ in range(30):
        if mon.abortRequested():
            return
        if win.getProperty("bp.update.state") == "avail":
            log("update autocheck: already offered")
            return
        if not xbmc.getCondVisibility("Window.IsActive(10000)"):
            reason = "home not active"
        elif xbmc.getCondVisibility("Player.HasVideo | Player.HasAudio"):
            reason = "player active"
        else:
            busy = [p for p in ("bp.confirm", "bp.notice", "bp.info", "bp.keys",
                                "bp.resume", "bp.del", "bp.rowmenu", "bp.srcq",
                                "bp.settings", "bp.power", "bp.about",
                                "bp.timer", "bp.rscan") if win.getProperty(p)]
            if not busy:
                reason = ""
                break
            reason = "overlay %s" % busy[0]
        if mon.waitForAbort(2):
            return
    if reason:
        log("update autocheck: skipped (%s)" % reason)
        return
    dev = _dev_update()
    if dev:
        latest, url = dev
    else:
        latest, url, _err = _github_latest()
    if not latest:
        log("update autocheck: no version from GitHub")
        return
    if _version_tuple(latest) <= _version_tuple(win.getProperty("bp.version") or ""):
        log("update autocheck: up to date (%s)" % latest)
        return
    log("update autocheck: offering %s" % latest)
    _set_avail(win, latest, url)
    update_confirm()


def updatebutton():
    """About update button: confirm+download the staged release, else check."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.update.state") == "avail":
        update_confirm()
    else:
        update_check()


def _confirm_reset_progress(win):
    """Drop a stale update progress bar before opening the confirm -- Window(10000)
    properties survive a skin reload, so a leftover bar would show too early."""
    win.clearProperty("bp.confirm.progress")
    win.clearProperty("bp.confirm.update")
    for i in range(1, 11):
        win.clearProperty("bp.confirm.f%d" % i)


def update_confirm():
    """Ask before downloading + installing the newer release (own confirm modal,
    focus on No). Yes runs `updateinstall` via the generic confirm handler."""
    win = xbmcgui.Window(10000)
    ver = win.getProperty("bp.update.ver") or ""
    win.clearProperty("bp.confirm.op")
    _confirm_reset_progress(win)
    win.setProperty("bp.confirm.title", xbmc.getLocalizedString(31535))
    line = xbmc.getLocalizedString(31536)
    try:
        line = line % ver
    except Exception:
        line = "%s %s" % (line, ver)
    win.setProperty("bp.confirm.line", line)
    # powerrun runs the update INLINE and keeps this modal open (progress bar).
    win.setProperty("bp.confirm.update", "1")
    win.setProperty("bp.confirm.from", xbmc.getInfoLabel("System.CurrentControlId"))
    win.setProperty("bp.confirm", "open")
    # Bounded retry: the overlay's `<visible>` may not be re-evaluated yet.
    for _ in range(10):
        xbmc.executebuiltin("SetFocus(956)")
        try:
            if xbmc.getCondVisibility("Control.HasFocus(956)"):
                break
        except Exception:
            break
        time.sleep(0.05)
    log("update: install confirm")


def _download_to(url, target, on_progress=None):
    """Stream `url` to `target`; (True, "") on a non-empty file, else
    (False, reason). A plain local path or `file://` URL (the dev override) is
    copied directly. `on_progress(done, total)` is called per chunk (total 0 when
    unknown). Removes a partial file on any error."""
    import shutil
    import urllib.request
    try:
        local = url[7:] if url.startswith("file://") else url
        if "://" not in local and os.path.isfile(local):
            shutil.copyfile(local, target)
            ok = os.path.getsize(target) > 0
            return ok, ("" if ok else "empty file")
        req = urllib.request.Request(url, headers={"User-Agent": "Browsybare"})
        with urllib.request.urlopen(req, timeout=120) as r:
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            done = 0
            with open(target, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        ok = os.path.getsize(target) > 0
        return ok, ("" if ok else "empty file")
    except Exception as e:
        log("update download failed: %s" % e)
        reason = "%s: %s" % (type(e).__name__, e)
        try:
            os.remove(target)
        except OSError:
            pass
        return False, reason


def _temp_dir():
    """A writable staging folder for the updater, probe-written. Kodi's temp
    first, then the OS temp dir, then Kodi's userdata/home -- a sandboxed Kodi
    (Android/Fire TV) can refuse the first two while the skin already writes its
    own state under userdata, so that is always usable. "" only if none work."""
    import tempfile
    cands = []
    for sp in ("special://temp", "special://userdata", "special://home"):
        try:
            cands.append(xbmcvfs.translatePath(sp))
        except Exception:
            pass
    try:
        cands.append(tempfile.gettempdir())
    except Exception:
        pass
    for d in cands:
        try:
            if not d:
                continue
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, "browsybare-probe.tmp")
            with open(probe, "wb") as f:
                f.write(b"1")
            os.remove(probe)
            log("update: temp dir ok")
            return d
        except Exception as e:
            log("update: temp candidate failed: %s" % e)
            continue
    log("update: no writable temp dir found")
    return ""


def _install_zip(zip_path, dest_root, ver=None):
    """Copy a release zip (single `browsybare/` root) over `dest_root` -- this
    running skin. Verifies the addon id/version, stages the whole zip first and
    rolls back replaced files from a backup on a copy error, so a broken
    download can never leave a half-updated skin. Returns (ok, error)."""
    import shutil
    import xml.etree.ElementTree as ET
    import zipfile
    tmp = _temp_dir()
    if not tmp:
        return False, "no writable temp folder"
    try:
        zf = zipfile.ZipFile(zip_path)
    except Exception as e:
        return False, "zip open: %s" % e
    stage = os.path.join(tmp, "browsybare-update")
    backup = os.path.join(tmp, "browsybare-backup")
    try:
        names = zf.namelist()
        stage_real = os.path.realpath(stage)
        # Zip names use "/", but a "\" is also a separator on Windows: normalize
        # before the traversal/layout checks so "browsybare/..\\..\\x" cannot
        # escape the stage.
        tops = {n.replace("\\", "/").split("/")[0]
                for n in names if n and not n.startswith(("/", "\\"))}
        if tops != {"browsybare"}:
            return False, "unexpected zip layout"
        raw = zf.read("browsybare/addon.xml").decode("utf-8", "replace")
        # ElementTree is not entity-expansion hardened; reject a DOCTYPE.
        if "<!DOCTYPE" in raw or "<!ENTITY" in raw:
            return False, "unsafe addon.xml"
        el = ET.fromstring(raw)
        if el.get("id") != "browsybare":
            return False, "wrong addon id: %r" % el.get("id")
        if ver and el.get("version") != ver:
            return False, "version mismatch: %s != %s" % (el.get("version"), ver)
        # Stage: extract the whole zip before touching the live addon.
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage, exist_ok=True)
        for n in names:
            parts = [p for p in n.replace("\\", "/").split("/") if p not in ("", ".")]
            if ".." in parts:
                return False, "unsafe zip path"
            out = os.path.join(stage, *parts)
            if not os.path.realpath(out).startswith(stage_real + os.sep):
                return False, "unsafe zip path"
            if n.endswith("/"):
                os.makedirs(out, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with zf.open(n) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except Exception as e:
        return False, "stage: %s" % e
    finally:
        zf.close()
    new_root = os.path.join(stage, "browsybare")
    if not os.path.isfile(os.path.join(new_root, "addon.xml")):
        return False, "no addon.xml in zip"
    # Copy over the live addon; keep a backup of every replaced file.
    shutil.rmtree(backup, ignore_errors=True)
    replaced = []
    try:
        for dirpath, _dirs, files in os.walk(new_root):
            for fn in files:
                src = os.path.join(dirpath, fn)
                rel = os.path.relpath(src, new_root)
                dst = os.path.join(dest_root, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    b = os.path.join(backup, rel)
                    os.makedirs(os.path.dirname(b), exist_ok=True)
                    shutil.copy2(dst, b)
                    replaced.append(rel)
                shutil.copy2(src, dst)
    except Exception as e:
        for rel in replaced:
            b = os.path.join(backup, rel)
            if os.path.exists(b):
                try:
                    shutil.copy2(b, os.path.join(dest_root, rel))
                except Exception:
                    pass
        return False, "copy: %s" % e
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
    return True, ""


def _upd_status(win, sid, value=None):
    """Update status text: the confirm modal's line when the update modal is up
    (bp.confirm.update), else the About pill label."""
    text = xbmc.getLocalizedString(sid)
    if value is not None:
        try:
            text = text % value
        except Exception:
            text = "%s %s" % (text, value)
    if win.getProperty("bp.confirm.update") == "1":
        win.setProperty("bp.confirm.line", text)
    else:
        win.setProperty("bp.update.text", text)


def _upd_bar(win, pct):
    """Light the confirm modal's 10-segment progress bar (0-100)."""
    filled = int(max(0, min(100, int(pct))) // 10)
    for i in range(1, 11):
        win.setProperty("bp.confirm.f%d" % i, "1" if i <= filled else "")


def _update_fail(win, sid, reason):
    """Report an update failure: a notification always, plus the confirm modal
    line (buttons back) when the update modal is up."""
    msg = "%s: %s" % (xbmc.getLocalizedString(sid), reason)
    _notify_update(msg)
    if win.getProperty("bp.confirm.update") == "1":
        win.setProperty("bp.confirm.line", msg)
        win.clearProperty("bp.confirm.progress")
        win.clearProperty("bp.confirm.update")
        for i in range(1, 11):
            win.clearProperty("bp.confirm.f%d" % i)
        for _ in range(8):
            xbmc.executebuiltin("SetFocus(956)")
            try:
                if xbmc.getCondVisibility("Control.HasFocus(956)"):
                    break
            except Exception:
                break
            time.sleep(0.15)
    else:
        _update_result(win, sid)


def update_install():
    """Download the newer release zip into Kodi's temp folder, then copy it over
    this addon folder and reload the skin -- the whole update runs in-skin, so it
    works wherever Kodi can write (`special://temp`): no manual "Install from
    zip" and no Downloads folder a sandboxed Kodi cannot see. When launched from
    the update confirm (bp.confirm.update) the confirm modal stays open and shows
    the phase text + progress bar instead of the About pill."""
    win = xbmcgui.Window(10000)
    url = win.getProperty("bp.update.url")
    ver = win.getProperty("bp.update.ver") or "latest"
    _upd_status(win, 31532, ver)  # "Version %s is being downloaded"
    _upd_bar(win, 0)
    t0 = time.time()
    tmp = _temp_dir()
    safe_ver = re.sub(r"[^A-Za-z0-9._-]", "_", ver)   # a tag may contain "/"
    zip_path = os.path.join(tmp, "browsybare-%s.zip" % safe_ver) if tmp else ""

    def _prog(done, total):
        if total:
            _upd_bar(win, done * 100 // total)

    if not url:
        ok, reason = False, "no download URL"
    elif not tmp:
        ok, reason = False, "no writable temp folder"
    else:
        ok, reason = _download_to(url, zip_path, on_progress=_prog)
    _hold(t0, UPDATE_PHASE_HOLD)
    for p in ("bp.update.state", "bp.update.ver", "bp.update.url"):
        win.clearProperty(p)
    if not ok:
        _update_fail(win, 31534, reason)  # "Download failed"
        return
    _upd_status(win, 31541, ver)  # "Version %s is being installed"
    _upd_bar(win, 100)
    t1 = time.time()
    good, err = _install_zip(zip_path, skin_root(), ver)
    _hold(t1, UPDATE_PHASE_HOLD)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    if not good:
        log("update install failed: %s" % (err or "?"))
        _update_fail(win, 31543, err or "?")  # "Install failed"
        return
    log("update install: %s copied into the addon folder" % ver)
    _upd_status(win, 31542, ver)  # "Version %s installed"
    # New files are on disk; let Kodi re-read them. UpdateLocalAddons refreshes
    # the addons db; ReloadSkin re-reads the skin XML (the reloaded Home boot
    # then re-merges the Estuary base layer). The label shows briefly first.
    try:
        xbmc.executebuiltin("UpdateLocalAddons")
    except Exception:
        pass
    time.sleep(2)
    try:
        # ReloadSkin does NOT clear Window(10000) properties, so drop the whole
        # update confirm (it would otherwise reappear on the reloaded Home).
        win.clearProperty("bp.about")
        win.clearProperty("bp.confirm")
        win.clearProperty("bp.confirm.progress")
        win.clearProperty("bp.confirm.update")
        win.clearProperty("bp.confirm.title")
        win.clearProperty("bp.confirm.line")
        win.clearProperty("bp.confirm.from")
        for i in range(1, 11):
            win.clearProperty("bp.confirm.f%d" % i)
        xbmc.executebuiltin("ReloadSkin()")
    except Exception as e:
        log("update install: reload failed: %s" % e)


def _update_text(win, sid, value=None):
    text = xbmc.getLocalizedString(sid)
    if value is not None:
        try:
            text = text % value
        except Exception:
            text = "%s %s" % (text, value)
    try:
        win.setProperty("bp.update.text", text)
    except Exception:
        pass


def _update_result(win, sid, value=None):
    """Show the result for 3 s, then fall back to the default label. The gen
    token makes a newer click win over an older reset."""
    _update_text(win, sid, value)
    token = str(time.time())
    try:
        win.setProperty("bp.update.gen", token)
    except Exception:
        return
    time.sleep(3)
    try:
        if win.getProperty("bp.update.gen") == token:
            _update_text(win, 31522)
    except Exception:
        pass


def _video_player_id():
    """Active video player id via JSON-RPC, or None."""
    try:
        players = json.loads(xbmc.executeJSONRPC(
            '{"jsonrpc":"2.0","id":1,"method":"Player.GetActivePlayers"}')
        ).get("result", [])
    except Exception:
        return None
    for p in players:
        if isinstance(p, dict) and p.get("type") == "video":
            return p.get("playerid")
    return None


def _player_all():
    """(streams, pos, subs, spos, senabled) of the active video player via one
    combined Player.GetProperties call."""
    pid = _video_player_id()
    if pid is None:
        return [], -1, [], -1, False
    try:
        props = json.loads(xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "Player.GetProperties",
            "params": {"playerid": pid,
                       "properties": ["audiostreams", "currentaudiostream",
                                      "subtitles", "currentsubtitle",
                                      "subtitleenabled"]}}))
        ).get("result", {})
    except Exception:
        return [], -1, [], -1, False
    streams = [s for s in (props.get("audiostreams") or [])
               if isinstance(s, dict)]
    cur = (props.get("currentaudiostream") or {}).get("index")
    pos = next((n for n, s in enumerate(streams)
                if s.get("index") == cur), -1)
    if pos < 0 and streams:
        pos = 0
    subs = [s for s in (props.get("subtitles") or []) if isinstance(s, dict)]
    # Kodi lists external subtitles in different orders across versions, so
    # sort by rendered tag; selection still uses Kodi's own stream index.
    try:
        subs.sort(key=lambda s: _subtitle_tag(s).lower())
    except Exception:
        pass
    scur = (props.get("currentsubtitle") or {}).get("index")
    spos = next((n for n, s in enumerate(subs) if s.get("index") == scur), -1)
    return streams, pos, subs, spos, bool(props.get("subtitleenabled"))


def track_select(row):
    """Select a track from an INFO row: 'a:<index>' audio, 's:<index>' subtitle,
    's:off' off; only the current markers refresh."""
    try:
        n = int(row)
    except (ValueError, TypeError):
        return
    win = xbmcgui.Window(10000)
    t = win.getProperty("bp.info.%d.track" % n)
    if len(t) < 3 or t[1] != ":" or t[0] not in ("a", "s"):
        return
    kind, arg = t[0], t[2:]
    pid = _video_player_id()
    if pid is None:
        return
    params = {"playerid": pid}
    if kind == "a":
        if not arg.lstrip("-").isdigit():
            return
        method, params["stream"] = "Player.SetAudioStream", int(arg)
    elif arg == "off":
        method, params["subtitle"] = "Player.SetSubtitle", "off"
    elif arg.lstrip("-").isdigit():
        method = "Player.SetSubtitle"
        params["subtitle"] = int(arg)
        params["enable"] = True
    else:
        return
    try:
        xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
    except Exception:
        return
    time.sleep(0.4)
    _refresh_track_markers(win)
    log("track -> %s" % t)


def _refresh_track_markers(win):
    """Move the current markers (bp.info.N.current) to the matching rows."""
    streams, pos, subs, spos, senabled = _player_all()
    active_a = streams[pos].get("index") if 0 <= pos < len(streams) else None
    active_s = subs[spos].get("index") \
        if senabled and 0 <= spos < len(subs) else None
    for i in range(1, INFO_ROWS + 1):
        t = win.getProperty("bp.info.%d.track" % i)
        if not t:
            continue
        if t.startswith("a:"):
            hit = active_a is not None and t[2:] == str(active_a)
        elif t == "s:off":
            hit = not senabled
        else:
            hit = active_s is not None and t[2:] == str(active_s)
        if hit:
            win.setProperty("bp.info.%d.current" % i, "1")
        else:
            win.clearProperty("bp.info.%d.current" % i)


# Destructive OS actions: rows set bp.confirm.title + op; add the question line.
CONFIRM_LINE = {
    "Quit": 31404,
    "Powerdown": 31399,
    "Reset": 31400,
    "Suspend": 31401,
    "Hibernate": 31402,
}


def powerconfirm():
    """Open the confirm overlay before a destructive OS action (powerrun runs
    it on Yes); focus starts on No."""
    win = xbmcgui.Window(10000)
    _confirm_reset_progress(win)
    op = win.getProperty("bp.confirm.op") or "Powerdown"
    for i in range(1, 8):
        win.clearProperty("bp.confirm.cmd.%d" % i)
    win.setProperty("bp.confirm.line", xbmc.getLocalizedString(CONFIRM_LINE.get(op, 31403)))
    if not win.getProperty("bp.confirm.title"):
        win.setProperty("bp.confirm.title", xbmc.getLocalizedString(31310))
    win.setProperty("bp.confirm.from", xbmc.getInfoLabel("System.CurrentControlId"))
    win.setProperty("bp.confirm", "open")
    focus_control(956)
    log("power confirm: %s" % op)


def powergeneric():
    """Open the confirm overlay for an unknown menu entry (row already set
    title/line/cmd.N); powerrun executes the raw builtins."""
    win = xbmcgui.Window(10000)
    _confirm_reset_progress(win)
    win.clearProperty("bp.confirm.op")
    win.setProperty("bp.confirm.line", xbmc.getLocalizedString(31403))
    win.setProperty("bp.confirm.from", xbmc.getInfoLabel("System.CurrentControlId"))
    win.setProperty("bp.confirm", "open")
    focus_control(956)
    log("power confirm (generic): %s" % win.getProperty("bp.confirm.cmd.1"))


def powerrun():
    """Yes-handler: run the specific op (handshake) or the row's raw builtins."""
    win = xbmcgui.Window(10000)
    # Update confirm: run the update inline and keep the modal open as a progress
    # modal (the buttons hide; update_install drives the bar + status text).
    if win.getProperty("bp.confirm.update") == "1":
        win.setProperty("bp.confirm.progress", "1")
        update_install()
        return
    op = win.getProperty("bp.confirm.op")
    from_ctl = win.getProperty("bp.confirm.from")
    try:
        n = int(win.getProperty("bp.confirm.cmds") or "0")
    except (TypeError, ValueError):
        n = 0
    if not n:
        n = 7  # legacy rows without the count property: keep the old behavior
    cmds = [win.getProperty("bp.confirm.cmd.%d" % i) for i in range(1, n + 1)]
    cmds = [c for c in cmds if c]
    win.clearProperty("bp.confirm")
    win.clearProperty("bp.confirm.op")
    win.clearProperty("bp.confirm.title")
    win.clearProperty("bp.confirm.line")
    win.clearProperty("bp.confirm.from")
    win.clearProperty("bp.confirm.cmds")
    for i in range(1, 8):
        win.clearProperty("bp.confirm.cmd.%d" % i)
    _confirm_reset_progress(win)
    if op:
        power(op)
    else:
        win.clearProperty("bp.power")
        for c in cmds:
            xbmc.executebuiltin(c)
        # Generic (non-quitting) confirm: return focus to the opener so it does
        # not stay stuck on the now-hidden Yes button (e.g. the update download).
        try:
            if from_ctl.isdigit() and xbmc.getCondVisibility("Window.IsActive(10000)"):
                time.sleep(0.15)
                xbmc.executebuiltin("SetFocus(%s)" % from_ctl)
        except Exception:
            pass


def powercancel():
    """Cancel-handler: close and return focus to the opener row (if the menu is
    still open) or the file list. Blocked while an update is running."""
    win = xbmcgui.Window(10000)
    if win.getProperty("bp.confirm.progress") == "1":
        return
    back = win.getProperty("bp.confirm.from")
    win.clearProperty("bp.confirm")
    win.clearProperty("bp.confirm.from")
    _confirm_reset_progress(win)
    time.sleep(0.2)
    if win.getProperty("bp.power") == "open" and back.isdigit():
        xbmc.executebuiltin("SetFocus(%s)" % back)
    else:
        xbmc.executebuiltin("SetFocus(33)")


def poweroutside():
    """Click outside / Back on the confirm backdrop: cancel, EXCEPT for the update
    modal (which stays until Yes/No so it cannot be dismissed by accident)."""
    win = xbmcgui.Window(10000)
    if (win.getProperty("bp.confirm.progress") == "1"
            or win.getProperty("bp.confirm.update") == "1"):
        return
    powercancel()


def noticeopen():
    """Open the notice modal (bp.notice); caller (boot.py) sets head/rows/focus.
    Falls back to OK (862) when the requested control is not focusable."""
    win = xbmcgui.Window(10000)
    win.setProperty("bp.notice", "open")
    # Home onload clears the bp.notice gate; re-assert briefly so the notice wins.
    for _ in range(8):
        time.sleep(0.15)
        if win.getProperty("bp.notice") != "open":
            win.setProperty("bp.notice", "open")
    fid = (win.getProperty("bp.notice.focus") or "862").strip()
    win.clearProperty("bp.notice.focus")
    if not fid.isdigit():
        fid = "862"
    got = False
    for _ in range(6):
        xbmc.executebuiltin("SetFocus(%s)" % fid)
        time.sleep(0.08)
        if xbmc.getCondVisibility("Control.HasFocus(%s)" % fid):
            got = True
            break
    if not got:
        fid = "862"
        xbmc.executebuiltin("SetFocus(862)")
    log("notice open: %s (focus %s)" % (win.getProperty("bp.notice.head"), fid))


def noticeclose():
    """Close the notice modal and restore focus to the file list (disabled while
    open, so wait a frame for it to re-enable)."""
    win = xbmcgui.Window(10000)
    win.clearProperty("bp.notice")
    for _ in range(6):
        time.sleep(0.06)
        xbmc.executebuiltin("SetFocus(33)")
        if xbmc.getCondVisibility("Control.HasFocus(33)"):
            break
    log("notice closed")


# Shutdown timer: native AlarmClock routed through power() handshake (timerfire).
TIMER_STR = {5: 31414, 15: 31406, 30: 31407, 60: 31408, 90: 31413, 120: 31409}


def timeropen():
    """Open the shutdown-timer overlay and focus the top pill (975); the top
    pill is idle/cancel depending on a running alarm."""
    win = xbmcgui.Window(10000)
    win.setProperty("bp.timer.from", xbmc.getInfoLabel("System.CurrentControlId"))
    active = xbmc.getCondVisibility("System.HasAlarm(shutdowntimer)")
    if active:
        rem = xbmc.getInfoLabel("System.AlarmPos").strip()
        win.setProperty("bp.timer.cancel", xbmc.getLocalizedString(31411) % rem)
    else:
        win.setProperty("bp.timer.cancel", xbmc.getLocalizedString(31412))
    win.setProperty("bp.timer", "open")
    focus_control(975)
    log("timer menu open (active=%s)" % active)


def timerclose():
    """Close the timer overlay and return focus to the opener row or the file list."""
    win = xbmcgui.Window(10000)
    back = win.getProperty("bp.timer.from")
    win.clearProperty("bp.timer")
    win.clearProperty("bp.timer.from")
    win.clearProperty("bp.timer.cancel")
    time.sleep(0.2)
    if win.getProperty("bp.power") == "open" and back.isdigit():
        xbmc.executebuiltin("SetFocus(%s)" % back)
    else:
        xbmc.executebuiltin("SetFocus(33)")


def timerset(minutes):
    """Set the native shutdowntimer alarm to run timerfire after `minutes`,
    notify, then close."""
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return
    if minutes <= 0:
        return
    xbmc.executebuiltin("AlarmClock(shutdowntimer,RunScript(special://skin/scripts/main.py,timerfire),%d)" % minutes)
    try:
        xbmcgui.Dialog().notification(
            xbmc.getLocalizedString(31405),
            xbmc.getLocalizedString(TIMER_STR.get(minutes, 31405)),
            xbmcgui.NOTIFICATION_INFO, 3000)
    except Exception:
        pass
    log("shutdown timer set: %d min" % minutes)
    timerclose()


def timercancel():
    """Top pill: cancel a running shutdowntimer alarm and close."""
    if xbmc.getCondVisibility("System.HasAlarm(shutdowntimer)"):
        xbmc.executebuiltin("CancelAlarm(shutdowntimer,true)")
        log("shutdown timer cancelled")
        timerclose()


def timerfire():
    """AlarmClock command: power down through OUR pre-exit handshake."""
    power("Powerdown")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        # Input gate while the base layer copies (bp.sync.active): swallow user
        # navigation/overlay commands; boot internals and escape hatches pass.
        if cmd in ("nav", "up", "root", "goto", "open", "drive",
                   "drivetoggle", "dropdown_open", "search", "ctx", "ctxmenu",
                   "remscanopen", "remscanstart", "remscansave", "remscancancel",
                   "blockscanopen", "blocktoggle", "blocktoggleapp",
                    "infoopen", "infoclose", "trackselect",
                     "photoexif", "photoclose", "photostep", "photoplay",
                     "photoshow", "photomode", "photointerval",
                     "photorepeat", "photoshuffle",
                     "resetopen", "keysopen", "settings_tab", "updatecheck",
                     "updatebutton", "updateinstall", "updateautocheck",
                     "resumeyes", "resumeno", "resumecancel",
                   "remdeftoggle", "rowmenuopen", "rowmenutoggle",
                   "rowmenuremove", "rowmenuclose", "rowmenuedit", "intensity", "intensity_next",
                   "guisound", "guisound_next",
                   "rename", "delete",
                    "mkdir", "mkdircreate", "delconfirm", "bl", "bladd",
                    "blremove", "bltoggle", "dirsrc", "netsrc", "netcommit",
                     "netsrcnew", "netproto_next", "netro_toggle", "netsrcfield", "netsrcadd",
                    "netsrcclose", "netsrctest",
                   "pickopen", "pickselect",
                   "sort", "foldersfirst", "grid", "accent_next",
                   "themecycle",
                   "vizcycle",
                   "vizsettings",
                   "listbump"):
            try:
                if xbmcgui.Window(10000).getProperty("bp.sync.active") == "1":
                    log("sync active, ignore %s" % cmd)
                    sys.exit(0)
            except SystemExit:
                raise
            except Exception:
                pass
        if cmd == "sync":
            _run_sync()
        elif cmd == "selftest":
            import selftest
            selftest.run(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "drives":
            drives()
        elif cmd == "nav":
            # Rejoin the args (path may contain commas); log the DECODED path so
            # log() can mask inline credentials.
            raw = ",".join(sys.argv[2:])
            path = _decode(raw)
            log("nav arrival: %s" % redact(path))
            nav(path)
        elif cmd == "up":
            up()
        elif cmd == "search":
            search_refresh()
        elif cmd == "root":
            root()
        elif cmd == "loadcancel":
            loadcancel()
        elif cmd == "goto":
            goto(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "open":
            raw = ",".join(sys.argv[2:])
            path = _decode(raw)
            log("open arrival: %s" % redact(path))
            _fopen(path)
        elif cmd == "photoclose":
            _fphotoclose()
        elif cmd == "photostep":
            try:
                _fphotostep(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
            except ValueError:
                _fphotostep(1)
        elif cmd == "photoplay":
            _fphotoplay()
        elif cmd == "photoshow":
            _fphotoshow()
        elif cmd == "photoexif":
            photoexif()
        elif cmd == "photointerval":
            _fphotointerval()
        elif cmd == "photomode":
            _fphotomode()
        elif cmd == "photorepeat":
            _fphotorepeat()
        elif cmd == "photoshuffle":
            _fphotoshuffle()
        elif cmd == "ctx":
            _fctx(_decode(",".join(sys.argv[2:])))
        elif cmd == "ctxmenu":
            _fctx_current()
        elif cmd == "bl":
            blacklist_open()
        elif cmd == "bladd":
            blacklist_add()
        elif cmd == "blremove":
            blacklist_remove(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "bltoggle":
            blacklist_toggle(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "dirsrc":
            dirsrc_open()
        elif cmd == "netsrc":
            netsrc_open()
        elif cmd == "netcommit":
            netcommit()
        elif cmd == "netsrcnew":
            netsrcnew()
        elif cmd == "netproto_next":
            netproto_next()
        elif cmd == "netro_toggle":
            netro_toggle()
        elif cmd == "netsrcfield":
            netsrcfield(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "netsrcadd":
            netsrcadd()
        elif cmd == "netsrctest":
            netsrctest()
        elif cmd == "netsrcclose":
            netsrcclose()
        elif cmd == "remoteopen":
            remote_open()
        elif cmd == "remscanopen":
            remscan_open(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "blockscanopen":
            blockscanopen()
        elif cmd == "remscanstart":
            remscan_start()
        elif cmd == "remscansave":
            remscan_save()
        elif cmd == "remscancancel":
            remscan_cancel()
        elif cmd == "remdeftoggle":
            _toggle_default_key(sys.argv[2] if len(sys.argv) > 2 else "",
                                sys.argv[3] if len(sys.argv) > 3 else "")
        elif cmd == "blocktoggle":
            blocktoggle(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "blocktoggleapp":
            blocktoggleapp(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "infoopen":
            infoopen()
        elif cmd == "keysopen":
            keysopen()
        elif cmd == "openlink":
            openlink()
        elif cmd == "updatecheck":
            update_check()
        elif cmd == "updatebutton":
            updatebutton()
        elif cmd == "updateinstall":
            update_install()
        elif cmd == "updateautocheck":
            update_autocheck()
        elif cmd == "resumeyes":
            resumeyes()
        elif cmd == "resumeno":
            resumeno()
        elif cmd == "resumecancel":
            resumecancel()
        elif cmd == "infoclose":
            infoclose()
        elif cmd == "trackselect":
            track_select(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "rowmenuopen":
            rowmenu_open(sys.argv[2] if len(sys.argv) > 2 else "",
                         sys.argv[3] if len(sys.argv) > 3 else "",
                         sys.argv[4] if len(sys.argv) > 4 else "",
                         sys.argv[5] if len(sys.argv) > 5 else "")
        elif cmd == "rowmenutoggle":
            rowmenu_toggle()
        elif cmd == "rowmenuremove":
            rowmenu_remove()
        elif cmd == "rowmenuedit":
            rowmenu_edit()
        elif cmd == "rowmenuclose":
            rowmenu_close()
        elif cmd == "dirsrctoggle":
            dirsrc_toggle(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "scanarm":
            scan_arm(False)
        elif cmd == "scanreset":
            scan_arm(True)
        elif cmd == "delconfirm":
            _fdelconfirm()
        elif cmd == "drivelist":
            drivelist()
        elif cmd == "drive":
            drive(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "drivetoggle":
            drive_toggle(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "dropdown_open":
            dropdown_open()
        elif cmd == "accent_next":
            accent_next()
        elif cmd == "themecycle":
            _run_theme_next()
        elif cmd == "vizcycle":
            vizcycle()
        elif cmd == "vizinit":
            viz_init()
        elif cmd == "vizsettings":
            vizsettings()
        elif cmd == "intensity":
            intensity(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "intensity_next":
            intensity_next()
        elif cmd == "guisound":
            guisound(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "guisound_next":
            guisound_next()
        elif cmd == "settings_tab":
            settings_tab(sys.argv[2] if len(sys.argv) > 2 else "",
                         sys.argv[3] if len(sys.argv) > 3 else "")
        elif cmd == "accents":
            _run_accents()
        elif cmd == "sort":
            sort_cycle()
        elif cmd == "rename":
            _frename(_decode(",".join(sys.argv[2:])))
        elif cmd == "delete":
            _fdelete(_decode(",".join(sys.argv[2:])))
        elif cmd == "clipcopy":
            _fclip_copy(_decode(",".join(sys.argv[2:])))
        elif cmd == "clipcut":
            _fclip_cut(_decode(",".join(sys.argv[2:])))
        elif cmd == "clippaste":
            _fclip_paste()
        elif cmd == "mkdir":
            _fmkdir()
        elif cmd == "mkdircreate":
            _fmkdircreate()
        elif cmd == "grid":
            grid()
        elif cmd == "listbump":
            _bump_list()
            log("listbump: r param bumped + Container.Refresh")
        elif cmd == "pickopen":
            picker_open()
        elif cmd == "pickcancel":
            picker_cancel()
        elif cmd == "pickselect":
            picker_select()
        elif cmd == "pickadd":
            pickadd(_decode(",".join(sys.argv[2:])))
        elif cmd == "srcask":
            srcask(_decode(",".join(sys.argv[2:])))
        elif cmd == "srcadd":
            srcadd()
        elif cmd == "close_settings":
            win = xbmcgui.Window(10000)
            win.clearProperty("bp.settings")
            win.clearProperty("bp.menu")
            win.clearProperty("bp.drives")
            win.clearProperty("bp.settings.tab")
            log("close_settings: cleared overlays")
        elif cmd == "focusplay":
            _ffocusplay()
        elif cmd == "focusvideo":
            _ffocusvideo()
        elif cmd == "audioprev":
            _faudioprev()
        elif cmd == "audionext":
            _faudionext()
        elif cmd == "quit":
            power("Quit")
        elif cmd == "restart":
            power("RestartApp")
        elif cmd == "power":
            # Shutdown-menu rows route terminating builtins through the pre-exit handshake.
            power(sys.argv[2] if len(sys.argv) > 2 else "Quit")
        elif cmd == "poweropen":
            poweropen()
        elif cmd == "powerconfirm":
            powerconfirm()
        elif cmd == "powerrun":
            powerrun()
        elif cmd == "powergeneric":
            powergeneric()
        elif cmd == "powercancel":
            powercancel()
        elif cmd == "poweroutside":
            poweroutside()
        elif cmd == "notice":
            noticeopen()
        elif cmd == "noticeclose":
            noticeclose()
        elif cmd == "resetopen":
            _fresetopen(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "resetrun":
            _fresetrun()
        elif cmd == "resetclose":
            _fresetclose()
        elif cmd == "timeropen":
            timeropen()
        elif cmd == "timerclose":
            timerclose()
        elif cmd == "timerset":
            timerset(sys.argv[2] if len(sys.argv) > 2 else "0")
        elif cmd == "timercancel":
            timercancel()
        elif cmd == "timerfire":
            timerfire()
        elif cmd == "skip":
            skip(sys.argv[2] if len(sys.argv) > 2 else "fwd")
        elif cmd == "speed":
            speed()
        elif cmd == "refreshdelay":
            refreshdelay()
        elif cmd == "volset":
            import volume
            volume.set_level(sys.argv[2] if len(sys.argv) > 2 else "100")
        elif cmd == "volup":
            import volume
            volume.step(volume.KEY_STEP)
        elif cmd == "voldown":
            import volume
            volume.step(-volume.KEY_STEP)
        elif cmd == "volmute":
            import volume
            volume.toggle_mute()
        elif cmd == "focusplaying":
            _ffocusplaying()
        elif cmd == "audioshuffle":
            _faudioshuffle()
        elif cmd == "videoshuffle":
            _fvideoshuffle()
        else:
            log("unknown command: %r" % cmd)
    except Exception as e:
        # Log only: setting bp.status=error would hide the file manager until the
        # next sync, so one transient error must not brick the screen.
        log("ERROR: Skin script error (%s): %s" % (cmd, e))

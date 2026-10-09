#!/usr/bin/env python3
"""Rubric reset (settings eraser buttons 274-279 in xml/Custom1150.xml): Yes resets every setting of that rubric to its default, No closes. Defaults mirror the Home.xml onload fallbacks and boot migrations (absent Skin.HasSetting = default); rows with side effects reuse the row implementations."""
import xbmc
import xbmcgui

from common import log

RUBRICS = (274, 275, 276, 277, 278, 279)
RUBRIC_TITLES = {
    274: 31319,  # Appearance and colors
    275: 31320,  # Files and folders
    276: 31472,  # System and Kodi
    277: 31360,  # Audio
    278: 31362,  # Video
    279: 31474,  # Photo
}

ACCENT_DEFAULT = "FF8EB7D7"  # Home.xml fallback = accents.json L3 slot 1
LEVEL_DEFAULT = "L3"
SORT_DEFAULT = "name"
GUISOUND_DEFAULT = 4


def _win():
    return xbmcgui.Window(10000)


def _set_str(name, value):
    try:
        xbmc.executebuiltin("Skin.SetString(%s,%s)" % (name, value))
    except Exception:
        pass


def _has(name):
    try:
        return bool(xbmc.getCondVisibility("Skin.HasSetting(%s)" % name))
    except Exception:
        return False


def _set_bool(name, on):
    """Deterministic Skin.HasSetting write via a conditional toggle."""
    try:
        if _has(name) != bool(on):
            xbmc.executebuiltin("Skin.ToggleSetting(%s)" % name)
    except Exception:
        pass


def reset_open(rubric):
    """Open the reset confirm modal for a rubric (eraser button id)."""
    try:
        rubric = int(rubric)
    except (TypeError, ValueError):
        return
    if rubric not in RUBRICS:
        return
    win = _win()
    try:
        title = xbmc.getLocalizedString(RUBRIC_TITLES[rubric])
    except Exception:
        title = ""
    win.setProperty("bp.reset.rubric", str(rubric))
    win.setProperty("bp.reset.title", title or "")
    win.setProperty("bp.reset", "open")
    for _ in range(6):
        xbmc.executebuiltin("SetFocus(943)")
        try:
            if xbmc.getCondVisibility("Control.HasFocus(943)"):
                break
        except Exception:
            break
        try:
            import time
            time.sleep(0.08)
        except Exception:
            break
    log("reset open: rubric %d" % rubric)


def reset_close():
    """Close the reset modal and return focus to the eraser button."""
    win = _win()
    try:
        rubric = int(win.getProperty("bp.reset.rubric") or "0")
    except (TypeError, ValueError):
        rubric = 0
    win.clearProperty("bp.reset")
    win.clearProperty("bp.reset.rubric")
    win.clearProperty("bp.reset.title")
    if rubric in RUBRICS:
        try:
            import time
            for _ in range(6):
                xbmc.executebuiltin("SetFocus(%d)" % rubric)
                try:
                    if xbmc.getCondVisibility("Control.HasFocus(%d)" % rubric):
                        break
                except Exception:
                    break
                time.sleep(0.06)
        except Exception:
            pass
    log("reset closed")


def _reset_appearance():
    win = _win()
    _set_str("accent", ACCENT_DEFAULT)
    _set_str("accent.level", LEVEL_DEFAULT)
    win.setProperty("bp.accent.value", ACCENT_DEFAULT)
    win.setProperty("bp.accent.level", LEVEL_DEFAULT)
    import sync
    sync.accents()
    _set_str("theme", sync.THEME_DEFAULT)
    sync.themes(sync.THEME_DEFAULT)
    _set_bool("zebra.hidden", False)


def _reset_files():
    _set_bool("show.hidden", False)
    _set_bool("foldersize.on", False)
    _set_bool("netsize.on", False)
    _set_bool("folders.last", False)
    _set_str("sort", SORT_DEFAULT)
    import main
    main._bump_list()


def _reset_system():
    import main
    main.guisound(GUISOUND_DEFAULT)
    # Dimmer default mirrors Kodi: ON where Kodi has a screensaver configured.
    from common import kodi_screensaver_mode
    _set_bool("dim.screen", bool(kodi_screensaver_mode().strip()))
    _set_bool("update.autocheck", False)


def _reset_visualisation():
    """Audio rubric reset: also clear Kodi's active visualisation and the row label,
    so the "Fullscreen visualisation" row goes back to Off."""
    try:
        import json
        xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "Settings.SetSettingValue",
            "params": {"setting": "musicplayer.visualisation", "value": ""}}))
    except Exception:
        pass
    try:
        _win().setProperty("bp.viz.name", xbmc.getLocalizedString(31548))
    except Exception:
        pass


def _reset_audio():
    _set_bool("show.audiopath", False)
    _set_bool("audio.noscroll", False)
    _set_bool("audio.visualisation", False)
    _reset_visualisation()
    try:
        import json
        xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "Settings.SetSettingValue",
            "params": {"setting": "musicplayer.crossfade", "value": 0}}))
        _win().setProperty("bp.crossfade.name", xbmc.getLocalizedString(31548))
    except Exception:
        pass


def _reset_video():
    _set_bool("show.videopath", False)
    _set_bool("video.noscroll", False)
    _set_bool("video.norefreshdelay", False)
    import main
    main.refreshdelay()


def _reset_photo():
    _set_bool("show.photopath", False)
    _set_bool("photo.noscroll", False)
    _set_bool("photo.recursive", False)
    _set_str("photo.interval", 5)
    _set_str("photo.mode", 0)
    _set_str("photo.repeat", 1)
    _set_str("photo.shuffle", 0)


_RESETTERS = {
    274: _reset_appearance,
    275: _reset_files,
    276: _reset_system,
    277: _reset_audio,
    278: _reset_video,
    279: _reset_photo,
}


def reset_run():
    """Apply the stored rubric's defaults, then close the modal."""
    win = _win()
    try:
        rubric = int(win.getProperty("bp.reset.rubric") or "0")
    except (TypeError, ValueError):
        rubric = 0
    fn = _RESETTERS.get(rubric)
    if fn is None:
        reset_close()
        return
    fn()
    log("reset run: rubric %d" % rubric)
    reset_close()

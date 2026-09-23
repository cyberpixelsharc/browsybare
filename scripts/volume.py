#!/usr/bin/env python3
"""Perceptual volume mapping for the footer / OSD volume meter. Kodi maps the volume percentage LINEARLY to decibels (0 % = -60 dB, 100 % = 0 dB) and its native slider cannot be intercepted, so this module owns a VIRTUAL level (0-100) and drives Kodi through the inverse dB curve. State: bp.vol (virtual level 0-100), bp.vol.kodi (last applied percent).
"""
import json
import math

import xbmc
import xbmcgui

WIN = 10000
EXPONENT = 1.8
KEY_STEP = 2


def _win():
    return xbmcgui.Window(WIN)


def _kodi_percent():
    """Current Kodi volume percentage via JSON-RPC, or None when unknown (must NOT be treated as 100, else init/resync would force maximum volume)."""
    try:
        resp = xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "Application.GetProperties",
            "params": {"properties": ["volume"]}}))
        return int(json.loads(resp)["result"]["volume"])
    except Exception:
        return None


def to_kodi(level):
    """Virtual level 0-100 -> Kodi percent (inverse of Kodi's linear-dB curve)."""
    try:
        level = float(level)
    except (TypeError, ValueError):
        return 100
    if level <= 0:
        return 0
    if level >= 100:
        return 100
    p = 100.0 + (20.0 * EXPONENT / 0.6) * math.log10(level / 100.0)
    return max(0, min(100, int(round(p))))


def from_kodi(percent):
    """Kodi percent -> virtual level (inverse of to_kodi)."""
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        return 100
    if percent <= 0:
        return 0
    if percent >= 100:
        return 100
    db = 0.6 * percent - 60.0
    amp = 10.0 ** (db / 20.0)
    level = 100.0 * (amp ** (1.0 / EXPONENT))
    return max(0, min(100, int(round(level))))


def _publish(level):
    """Write bp.vol and mirror the level into the skin string `volume.level`, which the slider binds via Skin.Numeric (window properties are NOT int infos for sliders)."""
    w = _win()
    level = max(0, min(100, int(level)))
    w.setProperty("bp.vol", str(level))
    try:
        xbmc.executebuiltin("Skin.SetString(volume.level,%d)" % level)
    except Exception:
        pass


def level():
    raw = _win().getProperty("bp.vol")
    try:
        return int(raw) if raw not in (None, "") else 100
    except (TypeError, ValueError):
        return 100


def apply(level_value):
    """Set the virtual level: drive Kodi through the inverse dB curve and move the slider visual (bp.vol)."""
    level_value = max(0, min(100, int(level_value)))
    p = to_kodi(level_value)
    xbmc.executebuiltin("SetVolume(%d)" % p)
    _publish(level_value)
    _win().setProperty("bp.vol.kodi", str(p))


def set_level(value):
    apply(value)


def step(delta):
    apply(level() + int(delta))


def init():
    """Home boot: window properties do not survive a restart, so recover the virtual level from Kodi's stored volume (a failed read leaves it untouched)."""
    cur = _kodi_percent()
    if cur is None:
        return
    apply(from_kodi(cur))


def resync(cur=None):
    """External change (native keys/CEC/JSON-RPC): reflect it in the meter without fighting it."""
    w = _win()
    cur = _kodi_percent() if cur is None else int(cur)
    if cur is None:
        return
    try:
        expected = int(w.getProperty("bp.vol.kodi") or "-1")
    except (TypeError, ValueError):
        expected = -1
    if abs(cur - expected) <= 1:
        return
    _publish(from_kodi(cur))
    w.setProperty("bp.vol.kodi", str(cur))

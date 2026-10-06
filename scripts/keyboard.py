#!/usr/bin/env python3
"""Browsybare on-screen keyboard (bp.kb overlay, xml/IncludesKeyboard.xml).

Stateless RunScript invocations: every key reads state from window properties,
mutates and writes the labels back. OK dispatches by mode via window properties
(RunScript args would break on spaces/commas in filenames).
"""
import contextlib
import os
import sys
import time
import xbmc
import xbmcgui

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import safe_label, redact, path_enc, state_dir, log as _clog, focus_control

LOG = "[skin] "

# Layout by active Kodi language: QWERTZ for German, AZERTY for French, QWERTY
# otherwise. Row-major; slot N = KEYS[N-1]; 13 columns per row.
ROWS_DE = [
    "1234567890ß´~",
    "qwertzuiopü+{",
    "asdfghjklöä#[",
    "yxcvbnm,.-<@|",
]
ROWS_FR = [
    "1234567890)=€",
    "azertyuiop^${",
    "qsdfghjklmù*[",
    "wxcvbn,;:!.@|",
]
ROWS_ES = [
    "1234567890'¡<",
    "qwertyuiop`+{",
    "asdfghjklñ´ü[",
    "zxcvbnm,.-_@|",
]
ROWS_EN = [
    "1234567890-=€",
    "qwertyuiop[]§",
    "asdfghjkl;'\\°",
    "zxcvbnm,./·¨´",
]


def _layout_rows():
    """Layout follows the active Kodi language (QWERTZ/AZERTY/QWERTY)."""
    try:
        lang = (xbmc.getLanguage(xbmc.ISO_639_1) or "").lower()
    except Exception:
        lang = ""
    if lang.startswith("de"):
        return ROWS_DE
    if lang.startswith("fr"):
        return ROWS_FR
    if lang.startswith("es"):
        return ROWS_ES
    return ROWS_EN


def _keys():
    return [c for row in _layout_rows() for c in row]


# Shift layer per layout: keys SHOW and INSERT their shifted symbol when
# shift/caps is active.
SHIFT_DE = {
    # number row (German): Shift+1 = !
    "1": "!", "2": '"', "3": "§", "4": "$", "5": "%",
    "6": "&", "7": "/", "8": "(", "9": ")", "0": "=",
    "ß": "?", "´": "`",
    # punctuation / right-side keys (German shift + AltGr set)
    ",": ";", ".": ":", "-": "_", "<": ">",
    "+": "*", "#": "'", "@": "\\",
    # 13th column (password symbols)
    "~": "^", "{": "}", "[": "]", "|": "€",
}
SHIFT_EN = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
    "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
    "-": "_", "=": "+",
    ",": "<", ".": ">", "/": "?", ";": ":", "'": '"',
    "[": "{", "]": "}", "\\": "|",
    # 13th column + middle-dot/diaeresis keys (every shift slot distinct)
    "·": "¿", "¨": "¬", "€": "£", "§": "¶", "°": "±", "´": "`",
}
SHIFT_FR = {
    # number row (AZERTY symbols, digits stay unshifted by design)
    "1": "&", "2": "é", "3": '"', "4": "'", "5": "(",
    "6": "-", "7": "è", "8": "_", "9": "ç", "0": "à",
    ")": "°", "=": "+",
    # letter rows (upper via ch.upper, except the symbol keys)
    "^": "¨", "$": "£", "*": "µ",
    ",": "?", ";": "«", ".": "»", ":": "/", "!": "§",
    "@": "\\",
    # 13th column (password symbols, every slot distinct)
    "{": "}", "[": "]", "|": "~", "€": "#",
}
SHIFT_ES = {
    # number row (Spanish symbols, digits stay unshifted by design)
    "1": "!", "2": '"', "3": "·", "4": "$", "5": "%",
    "6": "&", "7": "/", "8": "(", "9": ")", "0": "=",
    "'": "?", "¡": "¿", "<": ">",
    # letter rows (upper via ch.upper, except the symbol keys)
    "`": "^", "+": "*", "´": "¨",
    ",": ";", ".": ":", "-": "~", "_": "#", "@": "\\",
    # 13th column (password symbols, euro only via the pipe key)
    "{": "}", "[": "]", "|": "€",
}


def _shift_table():
    rows = _layout_rows()
    if rows is ROWS_DE:
        return SHIFT_DE
    if rows is ROWS_FR:
        return SHIFT_FR
    if rows is ROWS_ES:
        return SHIFT_ES
    return SHIFT_EN


def _shifted(ch, upper):
    """Key label/character for `ch` in the given case (symbols via the active
    layout's shift table)."""
    if not upper:
        return ch
    return _shift_table().get(ch, ch.upper())


def _log(msg):
    _clog(msg)


def _win():
    return xbmcgui.Window(10000)


def _refresh_labels(upper):
    win = _win()
    for i, ch in enumerate(_keys()):
        win.setProperty("bp.kb.k%d" % (i + 1), _shifted(ch, upper))


def _case():
    """Case state: 0 normal, 1 one-shot (auto-revert), 2 caps lock; cycled by
    the case button (178)."""
    try:
        return int(_win().getProperty("bp.kb.case") or 0)
    except (TypeError, ValueError):
        return 0


def _set_case(n):
    n = n % 3
    _win().setProperty("bp.kb.case", str(n))
    _refresh_labels(n != 0)
    return n


def _refresh_list():
    win = _win()
    win.setProperty("bp.refresh", str(time.time()))
    xbmc.executebuiltin("Container.Refresh")


def open_kb(mode, path=""):
    """Open the keyboard; search prefills from search.query, rename from the
    target basename. Refused while bp.sync.active."""
    win = _win()
    try:
        if win.getProperty("bp.sync.active") == "1":
            _log("sync active, ignore keyboard open")
            return
    except Exception:
        pass
    mode = mode or "mkdir"
    if mode in ("blacklist", "blacklistedit"):
        title = xbmc.getLocalizedString(31346)
        prefill = ""
        if mode == "blacklist":
            # Fresh add: drop any edit target a cancelled edit left behind.
            win.clearProperty("bp.blacklist.edit")
        else:
            try:
                idx = int(win.getProperty("bp.blacklist.edit") or 0)
            except (TypeError, ValueError):
                idx = 0
            if idx:
                prefill = win.getProperty("bp.bl.%d" % idx) or ""
    elif mode == "neturl":
        title = xbmc.getLocalizedString(31449)
        prefill = "ftp://"
    elif mode == "netlabel":
        title = xbmc.getLocalizedString(31450)
        try:
            import sources as _sources
            prefill = _sources.netsrc_host(win.getProperty("bp.net.pending") or "")
        except Exception:
            prefill = ""
    elif mode in ("netname", "netserver", "netport", "netpath", "netuser", "netpass"):
        _field = {"netname": "name", "netserver": "server", "netport": "port",
                  "netpath": "path", "netuser": "user", "netpass": "pass"}[mode]
        title = xbmc.getLocalizedString(
            {"netname": 31453, "netserver": 31455, "netport": 31461, "netpath": 31460,
             "netuser": 31456, "netpass": 31457}[mode])
        prefill = win.getProperty("bp.netsrc." + _field) or ""
    elif mode == "search":
        title = xbmc.getLocalizedString(31342)
        prefill = xbmc.getInfoLabel("Window(10000).Property(search.query)") or ""
    elif mode == "rename":
        title = xbmc.getLocalizedString(31327)
        # Display only; the exact target travels encoded via bp.kb.path/
        # bp.rename.path. Network URLs carry percent-encoded names.
        try:
            import sources as _sources
            disp = _sources.url_display(path or "")
        except Exception:
            disp = path or ""
        prefill = safe_label(os.path.basename(disp.rstrip("/")))
    else:
        mode = "mkdir"
        title = xbmc.getLocalizedString(31329)
        prefill = ""
    win.setProperty("bp.kb.mode", mode)
    win.setProperty("bp.kb.path", path_enc(path or ""))
    win.setProperty("bp.kb.title", title)
    _set(prefill, len(prefill))
    win.setProperty("bp.kb.case", "0")
    _refresh_labels(False)
    win.setProperty("bp.kb", "open")
    # Focus the first key as soon as the overlay is visible (quick retry; an
    # AlarmClock would flash a notification).
    focus_control(130)
    _log("keyboard open: %s" % mode)


RETURN_FOCUS = {"search": 74, "blacklist": 272, "blacklistedit": 272,
                "rename": 33, "mkdir": 33, "neturl": 273, "netlabel": 273,
                "netname": 792, "netserver": 794, "netport": 800,
                "netpath": 799, "netuser": 795, "netpass": 796}


def close():
    """Close the keyboard and return focus to the opener (delayed)."""
    win = _win()
    mode = win.getProperty("bp.kb.mode")
    win.clearProperty("bp.kb")
    tgt = RETURN_FOCUS.get(mode)
    if tgt:
        time.sleep(0.3)
        xbmc.executebuiltin("SetFocus(%d)" % tgt)


def _settle_text(max_wait=2.0, quiet=0.2):
    """Read bp.kb.text once it stopped changing (each key press is its own
    process, so a fast OK could commit a shorter string); reads under the
    writer lock."""
    deadline = time.time() + max_wait
    prev, stable_since = None, None
    cur = ""
    while True:
        with _edit_lock():
            cur = _text()
        if cur != prev:
            prev, stable_since = cur, time.time()
        elif stable_since is not None and (time.time() - stable_since) >= quiet:
            return cur
        if time.time() >= deadline:
            return cur
        time.sleep(0.05)


def dispatch():
    """OK: hand the text to the mode's handler and close."""
    win = _win()
    mode = win.getProperty("bp.kb.mode")
    text = _settle_text()
    close()
    if mode in ("netname", "netserver", "netport", "netpath", "netuser", "netpass"):
        import sources as _sources
        _field = {"netname": "name", "netserver": "server", "netport": "port",
                  "netpath": "path", "netuser": "user", "netpass": "pass"}[mode]
        # Spaces are invalid in server/port: strip now so the modal shows the
        # cleaned value.
        value = _sources.netsrc_sanitize(_field, text.strip())
        if _field == "server":
            # A pasted whole URL is split into dedicated fields (only empty
            # ones; explicit values win).
            host, port, sub = _sources.netsrc_split(value)
            value = host
            if port and not (win.getProperty("bp.netsrc.port") or "").strip():
                win.setProperty("bp.netsrc.port", port)
            if sub and not (win.getProperty("bp.netsrc.path") or "").strip():
                win.setProperty("bp.netsrc.path", sub)
        win.setProperty("bp.netsrc." + _field, value)
        win.clearProperty("bp.netsrc.test")  # an edit invalidates the last test
        if _field == "pass":
            win.setProperty("bp.netsrc.pass.mask", "••••••" if value else "")
        _log("keyboard: netsource field %s" % _field)
        return
    if mode == "search":
        if not text.strip():
            win.clearProperty("search.query")
            _refresh_list()
            _log("keyboard: search cleared")
            return
        win.setProperty("search.query", text)
        _refresh_list()
        # Land on the drive chip (the result list may be empty); re-assert
        # until it sticks (the overlay hide reassigns focus).
        for _ in range(6):
            time.sleep(0.15)
            xbmc.executebuiltin("SetFocus(30)")
            if xbmc.getCondVisibility("Control.HasFocus(30)"):
                break
        _log("keyboard: search '%s'" % redact(text))
        return
    if not text.strip():
        return
    if mode == "rename":
        win.setProperty("bp.rename.path", win.getProperty("bp.kb.path") or "")
        win.setProperty("bp.rename.name", text)
        xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,rename)")
    elif mode in ("blacklist", "blacklistedit"):
        win.setProperty("bp.blacklist.new", text)
        xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,bladd)")
    elif mode == "neturl":
        # Step 1 of the network-source flow: keep the URL, ask for the name.
        try:
            import sources as _sources
            valid = _sources.netsrc_valid(text)
        except Exception:
            valid = False
        if not valid:
            try:
                import xbmcgui as _gui
                _gui.Dialog().notification(
                    xbmc.getLocalizedString(31448) or "Browsybare",
                    xbmc.getLocalizedString(31451),
                    _gui.NOTIFICATION_ERROR, 4000)
            except Exception:
                pass
            _log("keyboard: netsource rejected '%s'" % redact(text.strip()))
            return
        win.setProperty("bp.net.pending", text.strip())
        open_kb("netlabel")
    elif mode == "netlabel":
        win.setProperty("bp.net.label", text)
        xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,netcommit)")
    elif mode == "mkdir":
        win.setProperty("bp.mkdir.name", text)
        xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,mkdircreate)")


def key(slot):
    """Character slot 1..len(keys) -> append (upper when shift is on)."""
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return
    keys = _keys()
    if slot < 1 or slot > len(keys):
        return
    case = _case()
    ch = _shifted(keys[slot - 1], bool(case))
    _insert(ch)
    # One-shot case (state 1): uppercase ONLY this character, then revert.
    if case == 1:
        _set_case(0)


def _text():
    return _win().getProperty("bp.kb.text") or ""


def _cursor():
    try:
        return int(_win().getProperty("bp.kb.cursor") or 0)
    except (TypeError, ValueError):
        return 0


def _set(text, cur):
    """Writes text + cursor and the field label (cursor "|" between the halves)."""
    win = _win()
    cur = max(0, min(cur, len(text)))
    win.setProperty("bp.kb.text", text)
    win.setProperty("bp.kb.cursor", str(cur))
    win.setProperty("bp.kb.textL", text[:cur])
    win.setProperty("bp.kb.textR", text[cur:])


@contextlib.contextmanager
def _edit_lock():
    """Cross-process lock around the keyboard text read-modify-write (each key
    press is its own process). A lock older than 2s is treated as stale."""
    lock = ""
    try:
        base = state_dir()
        lock = os.path.join(base, "kb.lock") if base else ""
    except Exception:
        lock = ""
    got = False
    if lock:
        for _ in range(60):  # up to ~3s
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                got = True
                break
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(lock) > 2.0:
                        os.remove(lock)
                        continue
                except OSError:
                    pass
                time.sleep(0.05)
            except OSError:
                break
    try:
        yield
    finally:
        if got:
            try:
                os.remove(lock)
            except OSError:
                pass


def _insert(ch):
    with _edit_lock():
        t, c = _text(), _cursor()
        _set(t[:c] + ch + t[c:], c + len(ch))


def _backspace():
    with _edit_lock():
        t, c = _text(), _cursor()
        if c > 0:
            _set(t[:c - 1] + t[c:], c - 1)


def _clipboard_text():
    """System clipboard text. Kodi's Python API has no clipboard access, so this
    shells out to the platform tool; Android/TV has no system clipboard, so the
    calls fail and "" is returned (the paste key is then a no-op)."""
    import platform
    import subprocess
    system = platform.system()
    if system == "Darwin":
        cmds = [["/usr/bin/pbpaste"], ["pbpaste"]]
    elif system == "Windows":
        cmds = [["powershell", "-NoProfile", "-Command", "Get-Clipboard"]]
    else:
        cmds = [["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "-b"]]
    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=2)
        except Exception:
            continue
        if res.returncode == 0:
            return res.stdout.decode("utf-8", "replace")
    return ""


def _paste_clipboard():
    """Insert the system clipboard into the field: the keyboard is single-line,
    so line breaks/tabs fold to a space and other control characters are
    dropped."""
    raw = _clipboard_text()[:50]
    out = []
    for ch in raw:
        if ch in "\r\n\t":
            out.append(" ")
        elif ch >= " ":
            out.append(ch)
    text = "".join(out).strip()
    if text:
        _insert(text)
    else:
        _log("keyboard: paste (clipboard empty)")


def _move(delta):
    with _edit_lock():
        _set(_text(), _cursor() + delta)


def _kb_open():
    return _win().getProperty("bp.kb") == "open"


def _audio_playing():
    """Background audio (no video): the browser must not jump while music
    plays."""
    try:
        return bool(xbmc.getCondVisibility("Player.HasAudio")
                    and not xbmc.getCondVisibility("Player.HasVideo"))
    except Exception:
        return False


def _is_video_osd_open():
    try:
        return bool(xbmc.getCondVisibility("Window.IsActive(VideoOSD)"))
    except Exception:
        return False


def _video_fullscreen():
    """Fullscreen video WITHOUT the OSD (the OSD is a separate dialog)."""
    try:
        return bool(xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"))
    except Exception:
        return False


SCAN_WINDOW = 3.0


def _scan_catch():
    """Scan pill (460) focus trap: swallow arrows briefly so the pressed key
    shows its code; Back/OK are never trapped."""
    try:
        if not xbmc.getCondVisibility("Control.HasFocus(460)"):
            return False
        try:
            t0 = float(_win().getProperty("bp.scan.t") or 0)
        except Exception:
            t0 = 0.0
        return (time.time() - t0) < SCAN_WINDOW
    except Exception:
        return False


# Overlay close stack for hardware Backspace/ESC in Home (topmost first; value
# is the focus target after close). Clears exactly one layer per press.
_OVERLAY_CLOSE = (
    ("bp.keys", "32"),
    ("bp.del", "33"),
    ("bp.ctx", "33"),
    ("bp.settings", "32"),
    ("bp.about", "32"),
    ("bp.menu", "32"),
    ("bp.drives", "30"),
)


def _close_top_overlay():
    """Close the topmost open Home overlay. Returns True when one closed."""
    win = _win()
    try:
        if win.getProperty("bp.reset") == "open":
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,resetclose)")
            _log("keyboard: hardware close -> bp.reset")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.rscan") == "open":
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,remscancancel)")
            _log("keyboard: hardware close -> bp.rscan")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.rowmenu") == "open":
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,rowmenuclose)")
            _log("keyboard: hardware close -> bp.rowmenu")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.confirm") == "open":
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,powercancel)")
            _log("keyboard: hardware close -> bp.confirm")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.resume") == "open":
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,resumecancel)")
            _log("keyboard: hardware close -> bp.resume")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.notice") == "open":
            win.clearProperty("bp.notice")
            time.sleep(0.2)
            xbmc.executebuiltin("SetFocus(33)")
            _log("keyboard: hardware close -> bp.notice")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.srcq") == "open":
            # Directory-source confirm modal: without this the keymap back
            # navigated behind it.
            win.clearProperty("bp.srcq")
            win.clearProperty("bp.srcq.path")
            win.clearProperty("bp.srcq.title")
            win.clearProperty("bp.srcq.line")
            time.sleep(0.2)
            xbmc.executebuiltin("SetFocus(33)")
            _log("keyboard: hardware close -> bp.srcq")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.timer") == "open":
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,timerclose)")
            _log("keyboard: hardware close -> bp.timer")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.power") == "open":
            win.clearProperty("bp.power")
            time.sleep(0.25)
            xbmc.executebuiltin("SetFocus(33)")
            _log("keyboard: hardware close -> bp.power")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.info") == "open":
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,infoclose)")
            _log("keyboard: hardware close -> bp.info")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.netsrc") == "open":
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,netsrcclose)")
            _log("keyboard: hardware close -> bp.netsrc")
            return True
    except Exception:
        pass
    try:
        if win.getProperty("bp.photo") == "open":
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,photoclose)")
            _log("keyboard: hardware close -> bp.photo")
            return True
    except Exception:
        pass
    for prop, focus in _OVERLAY_CLOSE:
        try:
            is_open = win.getProperty(prop) == "open"
        except Exception:
            is_open = False
        if is_open:
            win.clearProperty(prop)
            if prop == "bp.settings":
                # Settings is a custom DIALOG: closing the property alone leaves
                # it catching input.
                xbmc.executebuiltin("Dialog.Close(1150)")
                win.clearProperty("bp.settings.tab")
            if prop == "bp.ctx":
                win.clearProperty("bp.ctx.reduced")
            if focus:
                time.sleep(0.25)
                xbmc.executebuiltin("SetFocus(%s)" % focus)
            _log("keyboard: hardware close -> %s" % prop)
            return True
    try:
        picking = win.getProperty("bp.pick.active") == "1"
    except Exception:
        picking = False
    if picking:
        xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,pickcancel)")
        _log("keyboard: hardware close -> picker")
        return True
    return False


# Control id -> character slot (row-major over the 13-wide rows; a bare
# cid - 129 only works for row 1).
_KB_ID_SLOT = {}
for _r in range(4):
    for _c in range(12):
        _KB_ID_SLOT[130 + _r * 12 + _c] = _r * 13 + _c + 1
_KB_ID_SLOT.update({221: 13, 222: 26, 223: 39, 224: 52})


def _focused_kb_control():
    """Control id of the on-screen keyboard key holding focus, or None. Must
    use getCondVisibility (getInfoLabel("Control.HasFocus(N)") returns empty
    for bools)."""
    for cid in list(range(130, 187)) + [221, 222, 223, 224]:
        try:
            if xbmc.getCondVisibility("Control.HasFocus(%d)" % cid):
                return cid
        except Exception:
            continue
    return None


def _scan_armed():
    """True while the key scanner is armed: hardware keys must be swallowed so
    only kodi.log (read by the daemon) sees them."""
    try:
        return _win().getProperty("bp.scan.armed") == "1"
    except Exception:
        return False


def toggle_ctx():
    """Toggle our context menu overlay (Remote Menu / hardware c): open on the
    focused row, close with focus restore."""
    win = _win()
    if win.getProperty("bp.photo") == "open":
        # Photo viewer open: swallow Menu/right-click.
        _log("keyboard: ctx toggle ignored (photo viewer open)")
        return
    # Modal forms own the input: opening the native context menu would dismiss
    # them (its controls close via onback), so Menu/right-click is inert here.
    try:
        if (win.getProperty("bp.netsrc") == "open"
                or win.getProperty("bp.pick.active") == "1"):
            _log("keyboard: ctx toggle ignored (modal form open)")
            return
    except Exception:
        pass
    if win.getProperty("bp.ctx") == "open":
        win.clearProperty("bp.ctx")
        win.clearProperty("bp.ctx.reduced")
        time.sleep(0.25)
        xbmc.executebuiltin("SetFocus(33)")
        _log("keyboard: ctx toggle -> close")
    else:
        xbmc.executebuiltin("Action(ContextMenu)")


def special(tok):
    """Function keys: shift, space, back, del, esc, ok, cancel.

    Keymap calls arrive with the keyboard closed (Kodi 21 ignores keymap
    conditions), so the default Home behavior is reconstructed here; with the
    keyboard open, back deletes and ok selects the focused control. `del` is
    the on-screen backspace key: it only deletes (never closes)."""
    win = _win()
    if _scan_armed():
        _log("keyboard: special %s swallowed (scan armed)" % tok)
        return
    # Photo viewer: transport keys drive the slideshow, never the players.
    try:
        if win.getProperty("bp.photo") == "open" and not _kb_open():
            if tok in ("playpause", "space"):
                xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,photoplay)")
                return
            if tok in ("prev", "comma"):
                xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,photostep,-1)")
                return
            if tok in ("next", "period"):
                xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,photostep,1)")
                return
            if tok in ("stop", "x"):
                xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,photoclose)")
                return
    except Exception:
        pass
    if tok == "shift":
        # Cycle the case state: normal -> one-shot -> caps lock -> normal.
        if _kb_open():
            _set_case(_case() + 1)
    elif tok == "space":
        if _kb_open():
            _insert(" ")
        elif xbmc.getCondVisibility("Player.HasAudio|Player.HasVideo"):
            # Space toggles playback (honors the Remote-tab toggle); only while
            # a player runs.
            disabled = False
            try:
                import remotes as _rm
                disabled = _rm.key_disabled("playpause", "space")
            except Exception:
                pass
            if disabled:
                _log("keyboard: space transport disabled")
            else:
                xbmc.executebuiltin("Action(pause)")
    elif tok == "paste":
        # Paste key: insert the system clipboard into the field.
        if _kb_open():
            _paste_clipboard()
    elif tok == "del":
        # On-screen backspace key: delete only. An empty field must NOT close
        # the keyboard (that is the Cancel key / hardware Back / ESC).
        if _kb_open() and _cursor() > 0:
            _backspace()
    elif tok == "bsp":
        # Hardware Backspace: with the keyboard open it only deletes (an empty
        # field must not close it, like the on-screen delete key); otherwise it
        # keeps its Back behaviour (browse up, close overlays, ...).
        if _kb_open():
            if _cursor() > 0:
                _backspace()
        else:
            special("back")
    elif tok == "esc":
        # Hardware ESC: cancel the on-screen keyboard. Unlike Back it must NOT
        # delete (Backspace is the delete key) and must never fall through to
        # the core, which would close the dialog below the keyboard.
        if _kb_open():
            close()
        else:
            special("back")
    elif tok == "back":
        if not _kb_open() and win.getProperty("bp.listload") == "1":
            # A network source is loading (possibly an unreachable one): let
            # Back/ESC abort it instead of waiting out the VFS timeout.
            _log("keyboard: hardware back -> cancel list load")
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,loadcancel)")
        elif _kb_open():
            if _cursor() == 0:
                close()
            else:
                _backspace()
        elif _is_video_osd_open():
            # VideoOSD: close the overlay only, never stop playback.
            _log("keyboard: hardware back in VideoOSD -> close only")
            xbmc.executebuiltin("Action(back)")
        elif _video_fullscreen():
            # Fullscreen without OSD: open the OSD (native Back would leave it
            # playing invisibly).
            _log("keyboard: hardware back in fullscreen -> open OSD")
            xbmc.executebuiltin("ActivateWindow(VideoOSD)")
        elif not _close_top_overlay():
            try:
                home = xbmc.getCondVisibility("Window.IsActive(home)")
            except Exception:
                home = True
            if home and _audio_playing():
                # Locked while music plays (browsing up would jump behind the
                # footer).
                _log("keyboard: hardware back locked (audio playing)")
            elif home:
                _log("keyboard: hardware back with kb closed -> folder up")
                xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,up)")
            else:
                _log("keyboard: hardware back outside home -> Action(stop)")
                xbmc.executebuiltin("Action(stop)")
    elif tok in ("up", "down", "left", "right"):
        if _scan_catch():
            _log("keyboard: hardware %s caught by scan pill" % tok)
        else:
            xbmc.executebuiltin("Action(%s)" % tok)
    elif tok in ("cursor_left", "cursor_right"):
        # Caret buttons move the text cursor, not the control focus.
        if _kb_open():
            _move(-1 if tok == "cursor_left" else 1)
    elif tok == "ok":
        if _kb_open():
            # Hardware Enter = select the focused control (the keymap capture
            # would otherwise swallow it).
            cid = _focused_kb_control()
            slot = _KB_ID_SLOT.get(cid) if cid is not None else None
            if slot is not None:
                key(slot)
            elif cid == 180:
                special("back")
            elif cid == 178:
                special("shift")
            elif cid == 186:
                special("cursor_left")
            elif cid == 179:
                special("space")
            elif cid == 185:
                special("cursor_right")
            elif cid == 182:
                special("cancel")
            elif cid == 183:
                # Backdrop click-eater: its native action is special,cancel.
                special("cancel")
            else:
                dispatch()
        else:
            _log("keyboard: hardware ok with kb closed -> Action(select)")
            xbmc.executebuiltin("Action(select)")
    elif tok in ("prev", "comma"):
        if _kb_open():
            _insert(",")
        else:
            # system keyboard.xml binds comma -> SkipPrevious; lazy import
            # avoids the fileops cycle.
            from fileops import audio_step as _astep
            _astep("Previous")
    elif tok in ("next", "period"):
        if _kb_open():
            _insert(".")
        else:
            from fileops import audio_step as _astep
            _astep("Next")
    elif tok == "playpause":
        if _kb_open():
            _insert("p")
        else:
            xbmc.executebuiltin("PlayerControl(Play)")
    elif tok == "stop":
        if _kb_open():
            _insert("x")
        else:
            xbmc.executebuiltin("Action(stop)")
    elif tok == "rewind":
        if _kb_open():
            _insert("r")
        else:
            xbmc.executebuiltin("PlayerControl(SmallSkipBackward)")
    elif tok == "forward":
        if _kb_open():
            _insert("f")
        else:
            xbmc.executebuiltin("PlayerControl(SmallSkipForward)")
    elif tok == "mute":
        if _kb_open():
            _insert("m")
        else:
            xbmc.executebuiltin("Mute()")
    elif tok == "vol_up":
        import volume
        volume.step(volume.KEY_STEP)
    elif tok == "vol_down":
        import volume
        volume.step(-volume.KEY_STEP)
    elif tok == "menu":
        # Remote Menu key: toggle the context menu overlay when closed.
        if not _kb_open():
            toggle_ctx()
    elif tok == "settings":
        # Remote Setup key: toggle the settings dialog (1150) when closed.
        if not _kb_open():
            if xbmc.getCondVisibility("Window.IsActive(1150)"):
                xbmc.executebuiltin("Dialog.Close(1150)")
            else:
                win.clearProperty("bp.menu")
                win.setProperty("bp.settings", "open")
                xbmc.executebuiltin("ActivateWindow(1150)")
            _log("keyboard: settings key -> toggle dialog")
    elif tok == "power":
        # Remote Power key: toggle our Home shutdown overlay; honor the Remote/
        # block-list setting (the keymap routes it here unconditionally).
        try:
            import remotes as _rm
            if not _rm.effective_keys("power") or "power" in _rm.effective_blocks():
                _log("keyboard: power disabled -> inert")
                return
        except Exception:
            pass
        if xbmc.getCondVisibility("Window.IsActive(home)"):
            win = _win()
            if win.getProperty("bp.power") == "open":
                win.clearProperty("bp.power")
                time.sleep(0.2)
                xbmc.executebuiltin("SetFocus(33)")
            else:
                xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,poweropen)")
        else:
            # Not on Home: show our overlay instead of the native shutdown
            # dialog; stop playback first so a video does not run invisibly.
            try:
                if xbmc.getCondVisibility("Player.HasVideo") or xbmc.getCondVisibility("Player.HasAudio"):
                    xbmc.Player().stop()
            except Exception:
                pass
            win = _win()
            win.clearProperty("bp.power")  # never resurrect a stale overlay
            xbmc.executebuiltin("ActivateWindow(Home)")
            xbmc.executebuiltin(
                "AlarmClock(bp_poweropen,RunScript(special://skin/scripts/main.py,poweropen),00:01,silent)")
    elif tok == "cancel":
        if _kb_open():
            close()
    elif tok in ("home", "stophome", "roothome"):
        # Unified Home: close an open modal first, stop video/audio playback,
        # else jump to the source root.
        if _close_top_overlay():
            _log("keyboard: home closed an overlay")
        elif _video_fullscreen() or _is_video_osd_open():
            try:
                xbmc.Player().stop()
            except Exception:
                pass
            xbmc.executebuiltin("ActivateWindow(Home)")
            _log("keyboard: home in video -> stopped, back to Home")
        elif _audio_playing():
            try:
                xbmc.Player().stop()
            except Exception:
                pass
            _log("keyboard: hardware home stops audio")
        else:
            xbmc.executebuiltin("RunScript(special://skin/scripts/main.py,root)")


# Hardware typing tokens (ASCII-only RunScript args, no encoding risk): German
# shift symbols + umlauts.
HW_TOKENS = {
    "exc": "!", "dquot": '"', "sect": "§", "dollar": "$", "perc": "%",
    "amp": "&", "slash": "/", "lparen": "(", "rparen": ")", "equal": "=",
    "qmark": "?", "minus": "-", "underscore": "_", "plus": "+", "star": "*",
    "auml": "ä", "ouml": "ö", "uuml": "ü", "szlig": "ß",
    "Auml": "Ä", "Ouml": "Ö", "Uuml": "Ü",
}


def hardware(ch):
    """Hardware key while the on-screen keyboard is open: insert char if open,
    else ignore (prevents PVR shortcuts). Only `c` is special when closed
    (toggles the context menu); transport letters route through `special`."""
    win = _win()
    if _scan_armed():
        _log("keyboard: hardware %s swallowed (scan armed)" % ch)
        return
    if win.getProperty("bp.kb") != "open":
        if ch == "c":
            # Menu toggles our ctx overlay.
            toggle_ctx()
        return
    # ch may be a single char or a word like "space", handle space already via keymap
    if ch in HW_TOKENS:
        _insert(HW_TOKENS[ch])
        return
    if len(ch) == 1 and "a" <= ch <= "z":
        # On-screen case applies to hardware letters too (the keymap may
        # collapse Shift).
        case = _case()
        if case:
            _insert(ch.upper())
            if case == 1:
                _set_case(0)
            return
    if ch == " ":
        _insert(" ")
    elif len(ch) == 1:
        _insert(ch)
    elif ch == "space":
        _insert(" ")
    else:
        # fallback: insert as is (e.g. "," ".")
        _insert(ch)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "open":
            open_kb(sys.argv[2] if len(sys.argv) > 2 else "",
                    sys.argv[3] if len(sys.argv) > 3 else "")
        elif cmd == "key":
            key(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "special":
            special(sys.argv[2] if len(sys.argv) > 2 else "")
        elif cmd == "hardware":
            # RunScript splits args on commas, so rejoin argv[2:] for chars
            # like ",".
            raw = ",".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
            hardware(raw)
    except Exception as e:
        _log("keyboard error: %s" % e)

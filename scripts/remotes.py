#!/usr/bin/env python3
"""Remote functions to keymap (remotes/defaults.json -> keymaps/browsybare.xml).
Thin function->key map; the dispatcher keyboard.py decides per-window behaviour."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import log, read_json, read_text, skin_root, state_dir, write_json, write_text

DEFAULTS_FILE = "defaults.json"
USER_FILE = "remotes.json"

# Block marker -> ordered function ids. Device from the key name: obc* ->
# <universalremote>, else <keyboard>. OBC must NOT go into <remote> (code 0).
TRANSPORT = ["playpause", "stop", "rewind", "forward", "prev", "next",
             "vol_up", "vol_down", "mute"]
NAV = ["back", "home", "menu", "up", "down", "left", "right", "ok"]
ARROWS = ["up", "down", "left", "right"]
# Fullscreen video without OSD: arrows drive transport (up=next, down=prev,
# left=rewind, right=forward); other windows keep native arrow navigation.
FULLSCREEN_ARROW_ACTION = {"up": "next", "down": "prev",
                           "left": "rewind", "right": "forward"}
BLOCK_FUNCS = {
    "home_keyboard": NAV + TRANSPORT + ["settings", "power"],
    "home_remote": NAV + TRANSPORT + ["settings", "power"],
    # OSD: arrows stay native (focus navigation), locks keep the rest.
    "osd_keyboard": ["back", "home", "ok"] + TRANSPORT + ["power"],
    "osd_remote": ["back", "home", "ok"] + TRANSPORT + ["power"],
    # Fullscreen without OSD: arrows = transport (FULLSCREEN_ARROW_ACTION).
    "fullscreen_keyboard": ["back", "home"] + ARROWS + TRANSPORT + ["power"],
    "fullscreen_remote": ["back", "home"] + ARROWS + TRANSPORT + ["power"],
    # Photo viewer window (1151): transport only -- no typing, no arrows
    # (they navigate the OSD buttons), no rew/ff (photo OSD has none).
    "photo_keyboard": ["back", "ok", "playpause", "stop", "prev",
                       "next", "mute"],
    # Shutdown menu (window 10111): arrows/ok only (back stays native).
    "shutdown_keyboard": ARROWS + ["ok"],
    "shutdown_remote": ARROWS + ["ok"],
}
BLOCKS = tuple(BLOCK_FUNCS.keys())

# ---- block list (settings Remote tab, end) --------------------------------
# Void locks display-only; mouse buttons opt-in (default OFF); user scanner
# keys emit noop AFTER function mappings so a block wins in every window.
MOUSE_BUTTONS = ["leftclick", "middleclick", "rightclick"]
# Standard block-list entries ON by default: spare app/colored buttons +
# channel P+/P- (OBC). The system keymap binds them too, so they need a noop.
APP_BLOCKS = ["obc21", "obc31", "obc32", "obc34", "obc44", "obc45"]
VOID_KEYS = ["myvideo", "mymusic", "mypictures", "mytv", "guide", "livetv",
             "liveradio", "recordedtv", "epgsearch", "red", "green", "yellow",
             "blue", "favorites", "display", "record", "eject", "print",
             "teletext", "subtitle", "language", "playlist", "clear", "hash",
             "star", "last", "info", "title", "obc14", "obc26"]


# Display families for the default rows (Remote tab order): keyboard first,
# AmRemote second, Meson-IR (obc) last. AmRemote = browser_*/menu/media names.
AMREMOTE_KEYS = {
    "browser_home", "browser_back", "homepage", "menu", "prev_track",
    "next_track", "volume_up", "volume_down", "volume_mute", "play_pause",
    "stop", "rewind", "fastforward",
}


def family_of(key):
    """Display family rank: 0 keyboard, 1 AmRemote, 2 Meson-IR."""
    if key.startswith("obc"):
        return 2
    if key in AMREMOTE_KEYS:
        return 1
    return 0


def sort_keys(keys):
    """Stable sort by display family (keyboard, AmRemote, Meson-IR)."""
    return sorted(keys, key=family_of)


# Function order for the settings Remote tab.
FUNCTION_ORDER = ["home", "menu", "settings", "back", "up", "down", "left",
                  "right", "ok", "playpause", "stop", "rewind", "forward",
                  "prev", "next", "vol_up", "vol_down", "mute", "power"]

_KEY_RE = re.compile(r"^[a-z0-9_]+$")

# Navigation maps to native actions (no Python spawn per key press); only keys
# needing real logic (back/ok/letters/scan) stay on the dispatcher.
_NATIVE_ACTION = {
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    # Select instead of the dispatcher's special ok: every on-screen keyboard
    # control has its own onclick, so native Select is equivalent.
    "ok": "Select",
}


# Keys whose dispatcher token differs from the function action: "space" types
# while the on-screen keyboard is open, else toggles playback.
_KEY_ACTION = {
    "space": "RunScript(special://skin/scripts/keyboard.py,special,space)",
    # Hardware Backspace: delete-only while the on-screen keyboard is open
    # (never closes it), Back behaviour otherwise -- see keyboard.special("bsp").
    "backspace": "RunScript(special://skin/scripts/keyboard.py,special,bsp)",
}


def _action(fn):
    if fn in _NATIVE_ACTION:
        return _NATIVE_ACTION[fn]
    return "RunScript(special://skin/scripts/keyboard.py,special,%s)" % fn


def _defaults():
    """Ordered {fn: {"name": .., "keys": [...]}} from remotes/defaults.json."""
    data = read_json(os.path.join(skin_root(), "remotes", DEFAULTS_FILE), None)
    funcs = data.get("functions") if isinstance(data, dict) else None
    if not isinstance(funcs, dict):
        return {}
    return funcs


def _user_data():
    """(additions, off) from userdata/skindev/remotes.json: additions are
    user-added keys, off are row-toggled off (kept but not emitted)."""
    data = read_json(os.path.join(state_dir(), USER_FILE), None)
    add, off = {}, {}
    if isinstance(data, dict):
        a, o = data.get("additions"), data.get("off")
        if isinstance(a, dict):
            for fn, keys in a.items():
                if isinstance(keys, list):
                    add[fn] = [k for k in keys if isinstance(k, str) and k]
        if isinstance(o, dict):
            for fn, keys in o.items():
                if isinstance(keys, list):
                    off[fn] = [k for k in keys if isinstance(k, str) and k]
    return add, off


def _user():
    return _user_data()[0]


def _write_user(add, off=None):
    off = off or {}
    defaults = _defaults()
    clean_add = {fn: keys for fn, keys in add.items() if keys}
    clean_off = {}
    for fn, keys in off.items():
        keep = [k for k in keys
                if k in clean_add.get(fn, [])
                or k in (defaults.get(fn) or {}).get("keys", [])]
        if keep:
            clean_off[fn] = keep
    path = os.path.join(state_dir(), USER_FILE)
    data = read_json(path, None)
    if not isinstance(data, dict):
        data = {}
    data["additions"] = clean_add
    data["off"] = clean_off
    write_json(path, data)


def _block_user():
    """(blocked, off) from remotes.json: enabled block keys and scanner-added
    keys toggled off (kept so their row stays visible)."""
    data = read_json(os.path.join(state_dir(), USER_FILE), None)
    blocked, off = [], []
    if isinstance(data, dict):
        b, o = data.get("blocked"), data.get("blocked_off")
        if isinstance(b, list):
            blocked = [k for k in b if isinstance(k, str) and k]
        if isinstance(o, list):
            off = [k for k in o if isinstance(k, str) and k]
    return blocked, off


def _write_block_user(blocked, off):
    path = os.path.join(state_dir(), USER_FILE)
    data = read_json(path, None)
    if not isinstance(data, dict):
        data = {}
    data["blocked"] = blocked
    data["blocked_off"] = off
    write_json(path, data)


def block_device(key):
    """'mouse' | 'remote' | 'keyboard' for a block key."""
    if key in MOUSE_BUTTONS:
        return "mouse"
    if key.startswith("obc") or key in AMREMOTE_KEYS or key in VOID_KEYS:
        return "remote"
    return "keyboard"


def block_state(key, blocked=None, off=None):
    """True when a block key is enabled (APP_BLOCKS default ON, mouse buttons
    OFF; explicit overrides win)."""
    if blocked is None or off is None:
        blocked, off = _block_user()
    if key in off:
        return False
    if key in blocked:
        return True
    return key in APP_BLOCKS


def block_rows():
    """[(key, kind, on)] for the settings block rubric: kind is 'app', 'mouse'
    or 'user'."""
    blocked, off = _block_user()
    rows = [(k, "app", block_state(k, blocked, off)) for k in APP_BLOCKS]
    rows += [(k, "mouse", block_state(k, blocked, off)) for k in MOUSE_BUTTONS]
    std = set(APP_BLOCKS) | set(MOUSE_BUTTONS)
    users = ([k for k in blocked if k not in std]
             + [k for k in off if k not in std])
    rows += [(k, "user", block_state(k, blocked, off)) for k in users]
    return rows


def effective_blocks():
    """Enabled block keys: default-ON app buttons + explicit blocked, minus OFF."""
    blocked, off = _block_user()
    result = []
    for key in list(APP_BLOCKS) + list(blocked):
        if key not in off and key not in result:
            result.append(key)
    return result


def toggle_block(key):
    """Flip a block key. The read-only void locks stay ON. Returns new state."""
    if key in VOID_KEYS:
        return True
    blocked, off = _block_user()
    currently_on = block_state(key, blocked, off)
    if currently_on:
        if key in blocked:
            blocked.remove(key)
        if key not in off:
            off.append(key)
    else:
        if key in off:
            off.remove(key)
        if key not in blocked:
            blocked.append(key)
    _write_block_user(blocked, off)
    return not currently_on


def add_block(key):
    """Add a scanner key to the block list (enabled). Returns (ok, error)."""
    key = (key or "").strip().lower()
    if not key or not _KEY_RE.match(key):
        return False, "bad key code"
    blocked, off = _block_user()
    if key not in blocked:
        blocked.append(key)
    if key in off:
        off.remove(key)
    _write_block_user(blocked, off)
    return True, ""


def remove_block(key):
    """Drop a user-added block key. Returns True when changed."""
    blocked, off = _block_user()
    changed = False
    if key in blocked and key not in MOUSE_BUTTONS:
        blocked.remove(key)
        changed = True
    if key in off:
        off.remove(key)
        changed = True
    if changed:
        _write_block_user(blocked, off)
    return changed


def unblock(key):
    """Force a key OFF in the block list. A default-ON app key needs an explicit
    `off` entry (removing it from `blocked` alone keeps it ON)."""
    blocked, off = _block_user()
    changed = False
    if key in blocked:
        blocked.remove(key)
        changed = True
    if key in APP_BLOCKS and key not in off:
        off.append(key)
        changed = True
    if changed:
        _write_block_user(blocked, off)




def _known_keys(fn, defaults=None, user=None):
    """Default + user-added keys of a function (dedup, order)."""
    defaults = _defaults() if defaults is None else defaults
    user = _user() if user is None else user
    keys = [k for k in ((defaults.get(fn) or {}).get("keys") or [])
            if isinstance(k, str) and k]
    for k in user.get(fn, []):
        if k not in keys:
            keys.append(k)
    return keys


def key_on(fn, key):
    """True when a default or user-added key is enabled (not toggled off)."""
    _, off = _user_data()
    return key in _known_keys(fn) and key not in off.get(fn, [])


def key_disabled(fn, key):
    """True only when `key` is a known key of `fn` that the user toggled off."""
    _, off = _user_data()
    return key in _known_keys(fn) and key in off.get(fn, [])


def toggle_key(fn, key):
    """Enable/disable a default or user-added key. Returns the new state (True = on)."""
    add, off = _user_data()
    if key not in _known_keys(fn):
        return True
    off.setdefault(fn, [])
    if key in off[fn]:
        off[fn].remove(key)
    else:
        off[fn].append(key)
    _write_user(add, off)
    return key not in off.get(fn, [])


def effective_keys(fn, defaults=None, user=None, off=None):
    """Default keys first, then ENABLED user additions (dedup, order)."""
    defaults = _defaults() if defaults is None else defaults
    if user is None or off is None:
        _add, _off = _user_data()
        user = _add if user is None else user
        off = _off if off is None else off
    prof = defaults.get(fn) or {}
    disabled = set(off.get(fn, []))
    keys = sort_keys([k for k in (prof.get("keys") or [])
                      if isinstance(k, str) and k and k not in disabled])
    for k in user.get(fn, []):
        if k not in keys and k not in disabled:
            keys.append(k)
    return keys


def function_rows():
    """Ordered [(fn, name, default_keys, user_keys)] for the settings tab;
    user_keys includes disabled ones."""
    defaults = _defaults()
    user = _user()
    rows = []
    for fn in FUNCTION_ORDER:
        prof = defaults.get(fn) or {}
        name = prof.get("name") or fn
        dkeys = sort_keys([k for k in (prof.get("keys") or [])
                           if isinstance(k, str) and k])
        ukeys = [k for k in user.get(fn, []) if k and k not in dkeys]
        rows.append((fn, name, dkeys, ukeys))
    return rows


def add_key(fn, key):
    """Add a user key to a function. A key lives in ONE function: conflicting
    user additions move, active defaults elsewhere auto-disable. (ok, error)."""
    key = (key or "").strip().lower()
    defaults = _defaults()
    if fn not in defaults:
        return False, "unknown function"
    if not key or not _KEY_RE.match(key):
        return False, "bad key code"
    add, off = _user_data()
    is_default_here = key in (defaults.get(fn, {}).get("keys") or [])
    # 1. Auto-disable an active default of another function owning the key.
    for other_fn, prof in defaults.items():
        if other_fn != fn and key in (prof.get("keys") or []):
            off.setdefault(other_fn, [])
            if key not in off[other_fn]:
                off[other_fn].append(key)
    # 2. Move a user addition away from other functions.
    for other in list(add.keys()):
        if other == fn:
            continue
        add[other] = [k for k in add[other] if k != key]
        if not add[other]:
            del add[other]
    # 3. Drop stale `off` entries (keep step-1 default disables).
    for other in list(off.keys()):
        if other == fn:
            continue
        if key in off[other] and key not in (defaults.get(other, {}).get("keys") or []):
            off[other] = [k for k in off[other] if k != key]
            if not off[other]:
                del off[other]
    # 4. Enable it in the target function.
    if not is_default_here:
        add.setdefault(fn, [])
        if key not in add[fn]:
            add[fn].append(key)
    off[fn] = [k for k in off.get(fn, []) if k != key]  # claimed = enabled
    if not off.get(fn):
        off.pop(fn, None)
    _write_user(add, off)
    unblock(key)  # an explicit assignment wins over a block-list noop
    return True, ""


def remove_key(fn, key):
    """Drop a user-added key from a function (True when changed); also re-enables
    a default elsewhere that add_key auto-disabled."""
    key = (key or "").strip().lower()
    add, off = _user_data()
    if not (fn in add and key in add[fn]):
        return False
    add[fn].remove(key)
    if not add[fn]:
        del add[fn]
    off[fn] = [k for k in off.get(fn, []) if k != key]
    if not off.get(fn):
        off.pop(fn, None)
    # If it is a default elsewhere and no longer assigned, clear its auto-disable.
    defaults = _defaults()
    for other_fn, prof in defaults.items():
        if other_fn == fn:
            continue
        if key in (prof.get("keys") or []) and key not in add.get(other_fn, []):
            if key in off.get(other_fn, []):
                off[other_fn] = [k for k in off[other_fn] if k != key]
                if not off[other_fn]:
                    off.pop(other_fn, None)
    _write_user(add, off)
    return True


# Hardware-typing key block -- SINGLE SOURCE, emitted into every
# <!--GEN:typing--> section (<Home> + window1150); Home's transport genes follow.
TYPING_LINES = [
    '<a>RunScript(special://skin/scripts/keyboard.py,hardware,a)</a>',
    '<b>RunScript(special://skin/scripts/keyboard.py,hardware,b)</b>',
    '<c>RunScript(special://skin/scripts/keyboard.py,hardware,c)</c>',
    '<d>RunScript(special://skin/scripts/keyboard.py,hardware,d)</d>',
    '<e>RunScript(special://skin/scripts/keyboard.py,hardware,e)</e>',
    '<g>RunScript(special://skin/scripts/keyboard.py,hardware,g)</g>',
    '<h>RunScript(special://skin/scripts/keyboard.py,hardware,h)</h>',
    '<i>RunScript(special://skin/scripts/keyboard.py,hardware,i)</i>',
    '<j>RunScript(special://skin/scripts/keyboard.py,hardware,j)</j>',
    '<k>RunScript(special://skin/scripts/keyboard.py,hardware,k)</k>',
    '<l>RunScript(special://skin/scripts/keyboard.py,hardware,l)</l>',
    '<n>RunScript(special://skin/scripts/keyboard.py,hardware,n)</n>',
    '<o>RunScript(special://skin/scripts/keyboard.py,hardware,o)</o>',
    '<q>RunScript(special://skin/scripts/keyboard.py,hardware,q)</q>',
    '<s>RunScript(special://skin/scripts/keyboard.py,hardware,s)</s>',
    '<t>RunScript(special://skin/scripts/keyboard.py,hardware,t)</t>',
    '<u>RunScript(special://skin/scripts/keyboard.py,hardware,u)</u>',
    '<v>RunScript(special://skin/scripts/keyboard.py,hardware,v)</v>',
    '<w>RunScript(special://skin/scripts/keyboard.py,hardware,w)</w>',
    '<y>RunScript(special://skin/scripts/keyboard.py,hardware,y)</y>',
    '<z>RunScript(special://skin/scripts/keyboard.py,hardware,z)</z>',
    '<f>RunScript(special://skin/scripts/keyboard.py,hardware,f)</f>',
    '<m>RunScript(special://skin/scripts/keyboard.py,hardware,m)</m>',
    '<p>RunScript(special://skin/scripts/keyboard.py,hardware,p)</p>',
    '<r>RunScript(special://skin/scripts/keyboard.py,hardware,r)</r>',
    '<x>RunScript(special://skin/scripts/keyboard.py,hardware,x)</x>',
    '<one>RunScript(special://skin/scripts/keyboard.py,hardware,1)</one>',
    '<two>RunScript(special://skin/scripts/keyboard.py,hardware,2)</two>',
    '<three>RunScript(special://skin/scripts/keyboard.py,hardware,3)</three>',
    '<four>RunScript(special://skin/scripts/keyboard.py,hardware,4)</four>',
    '<five>RunScript(special://skin/scripts/keyboard.py,hardware,5)</five>',
    '<six>RunScript(special://skin/scripts/keyboard.py,hardware,6)</six>',
    '<seven>RunScript(special://skin/scripts/keyboard.py,hardware,7)</seven>',
    '<eight>RunScript(special://skin/scripts/keyboard.py,hardware,8)</eight>',
    '<nine>RunScript(special://skin/scripts/keyboard.py,hardware,9)</nine>',
    '<zero>RunScript(special://skin/scripts/keyboard.py,hardware,0)</zero>',
    '<!-- Hardware Shift layer (typing while the on-screen keyboard is open;',
    '     closed it is swallowed like the plain letters). Uppercase travels',
    '     as plain ASCII (no encoding risk); shift symbols as tokens (German',
    '     hardware layout: !"§$%&/()=) mapped in keyboard.py HW_TOKENS.',
    '     Risk (Kodi wiki: shift alone may collapse to the plain key): then',
    '     the plain mapping fires instead, lowercase, no harm. -->',
    '<a mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,A)</a>',
    '<b mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,B)</b>',
    '<c mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,C)</c>',
    '<d mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,D)</d>',
    '<e mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,E)</e>',
    '<f mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,F)</f>',
    '<g mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,G)</g>',
    '<h mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,H)</h>',
    '<i mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,I)</i>',
    '<j mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,J)</j>',
    '<k mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,K)</k>',
    '<l mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,L)</l>',
    '<m mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,M)</m>',
    '<n mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,N)</n>',
    '<o mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,O)</o>',
    '<p mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,P)</p>',
    '<q mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,Q)</q>',
    '<r mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,R)</r>',
    '<s mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,S)</s>',
    '<t mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,T)</t>',
    '<u mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,U)</u>',
    '<v mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,V)</v>',
    '<w mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,W)</w>',
    '<x mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,X)</x>',
    '<y mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,Y)</y>',
    '<z mod="shift">RunScript(special://skin/scripts/keyboard.py,hardware,Z)</z>',
    '<!-- German umlauts via unicode key id (0xF000 | codepoint, Kodi wiki',
    '     key-id rule; no shift mod needed, each case is its own id).',
    '     Verify on the box via kodi.log (OnKey code); adjust if the',
    '     platform reports different ids. -->',
    '<key id="61668">RunScript(special://skin/scripts/keyboard.py,hardware,auml)</key>',
    '<key id="61686">RunScript(special://skin/scripts/keyboard.py,hardware,ouml)</key>',
    '<key id="61692">RunScript(special://skin/scripts/keyboard.py,hardware,uuml)</key>',
    '<key id="61663">RunScript(special://skin/scripts/keyboard.py,hardware,szlig)</key>',
    '<key id="61636">RunScript(special://skin/scripts/keyboard.py,hardware,Auml)</key>',
    '<key id="61654">RunScript(special://skin/scripts/keyboard.py,hardware,Ouml)</key>',
    '<key id="61660">RunScript(special://skin/scripts/keyboard.py,hardware,Uuml)</key>',
    '<!-- US-xkb position layer (CoreELEC default, Kodi keeps US libinput',
    '     xkb regardless of locale.keyboardlayouts): German HW keys arrive',
    '     with US symbols, so map by POSITION. Unshifted syms have Kodi',
    '     names; shifted syms travel as PLAIN key-id (0xF000|sym, log-proven:',
    '     exclaim->0xF021 fires without mod while mod=shift key-ids stay',
    '     dead). Plain äöüß',
    '     (quote/semicolon/opensquarebracket/minus), German minus via',
    '     forwardslash, shifted ÄÖÜ?_" via key-id (" also covers Shift+2,',
    '     typed as Ä by frequency). § and * ride plain key-ids too',
    '     (Shift+3 yields numbersign, Shift+plus-key yields rightbrace);',
    '     + already rides closesquarebracket/plus by name. -->',
    '<quote>RunScript(special://skin/scripts/keyboard.py,hardware,auml)</quote>',
    '<semicolon>RunScript(special://skin/scripts/keyboard.py,hardware,ouml)</semicolon>',
    '<opensquarebracket>RunScript(special://skin/scripts/keyboard.py,hardware,uuml)</opensquarebracket>',
    '<minus>RunScript(special://skin/scripts/keyboard.py,hardware,szlig)</minus>',
    '<forwardslash>RunScript(special://skin/scripts/keyboard.py,hardware,minus)</forwardslash>',
    '<equals>RunScript(special://skin/scripts/keyboard.py,hardware,equal)</equals>',
    '<!-- 61474 is the `"` sym (Shift+2 on German hardware, macOS + box):',
    '     insert dquot. Box Shift+Ä stays on its own umlaut id 61636, so the',
    '     old frequency mapping to Auml is gone. -->',
    '<key id="61474">RunScript(special://skin/scripts/keyboard.py,hardware,dquot)</key>',
    '<key id="61498">RunScript(special://skin/scripts/keyboard.py,hardware,Ouml)</key>',
    '<key id="61563">RunScript(special://skin/scripts/keyboard.py,hardware,Uuml)</key>',
    '<!-- 61503 is 0x3F `?` (German Shift+ß), 61535 is 0x5F `_` (German',
    '     Shift+minus): they were swapped, so `?` typed `_` and vice versa',
    '     on macOS and box alike. Corrected here. -->',
    '<key id="61535">RunScript(special://skin/scripts/keyboard.py,hardware,underscore)</key>',
    '<key id="61503">RunScript(special://skin/scripts/keyboard.py,hardware,qmark)</key>',
    '<key id="61473">RunScript(special://skin/scripts/keyboard.py,hardware,exc)</key>',
    '<key id="61476">RunScript(special://skin/scripts/keyboard.py,hardware,dollar)</key>',
    '<key id="61477">RunScript(special://skin/scripts/keyboard.py,hardware,perc)</key>',
    '<!-- Shift+3 yields numbersign (§), Shift+plus-key yields rightbrace (*):',
    '     plain key-id fallbacks for platforms that report shifted syms as',
    '     ids (box 1 reports names instead and swallows Shift+3 entirely,',
    '     so these stay silent there). Tokens live in HW_TOKENS. -->',
    '<key id="61475">RunScript(special://skin/scripts/keyboard.py,hardware,sect)</key>',
    '<key id="61565">RunScript(special://skin/scripts/keyboard.py,hardware,star)</key>',
    '<!-- Mac § candidates (Shift+3 types nothing there today): 61607 is',
    '     0xA7, and section is the guess for the Kodi name of the sym.',
    '     No existing entries, so zero regression risk either way. -->',
    '<key id="61607">RunScript(special://skin/scripts/keyboard.py,hardware,sect)</key>',
    '<section>RunScript(special://skin/scripts/keyboard.py,hardware,sect)</section>',
    '<!-- Box-proven arrivals (German syms, HandleKey log):',
    '     Shift+plus-key arrives as asterisk (must insert star), Shift+8 as',
    '     leftbracket/0x28 (must insert lparen, hence 61480 below), Shift+9',
    '     as rightbracket (rparen, correct). Shift+3 never reaches Kodi',
    '     (CoreELEC input layer swallows it, shift-160 corpse), so § stays',
    '     on-screen-only there. -->',
    '<asterisk>RunScript(special://skin/scripts/keyboard.py,hardware,star)</asterisk>',
    '<caret>RunScript(special://skin/scripts/keyboard.py,hardware,amp)</caret>',
    '<closesquarebracket>RunScript(special://skin/scripts/keyboard.py,hardware,plus)</closesquarebracket>',
    '<key id="61478">RunScript(special://skin/scripts/keyboard.py,hardware,amp)</key>',
    '<key id="61480">RunScript(special://skin/scripts/keyboard.py,hardware,lparen)</key>',
    '<key id="61481">RunScript(special://skin/scripts/keyboard.py,hardware,rparen)</key>',
    '<plus>RunScript(special://skin/scripts/keyboard.py,hardware,plus)</plus>',
    '<space>RunScript(special://skin/scripts/keyboard.py,special,space)</space>',
    '<enter mod="ctrl">RunScript(special://skin/scripts/keyboard.py,hardware,c)</enter>',
    '<return mod="ctrl">RunScript(special://skin/scripts/keyboard.py,hardware,c)</return>',
    '<!-- Shift+F10: the Windows/Linux keyboard context-menu standard -->',
    '<f10 mod="shift">RunScript(special://skin/scripts/keyboard.py,special,menu)</f10>',
]


def _block_body(name, defaults, user):
    # name ends with "_remote" -> OBC block in <universalremote>, else <keyboard>.
    device_remote = name.endswith("_remote")
    fullscreen = name.startswith("fullscreen")
    lines, seen = [], set()
    for fn in BLOCK_FUNCS.get(name, []):
        if fn not in defaults:
            continue
        # Fullscreen: the arrow keys act as transport (up=next, ...).
        if fullscreen and fn in FULLSCREEN_ARROW_ACTION:
            action = _action(FULLSCREEN_ARROW_ACTION[fn])
        # Back: ALWAYS through the dispatcher (special back), in Home too.
        # Native Back falls through to the core, which stops playing audio.
        else:
            action = _action(fn)
        for key in effective_keys(fn, defaults, user):
            if key.startswith("obc") != device_remote:
                continue
            if key in seen:
                continue
            seen.add(key)
            act = _KEY_ACTION.get(key, action)
            lines.append("      <%s>%s</%s>" % (key, act, key))
    # User block list emitted AFTER the function mappings (Kodi keeps the
    # later duplicate), so a blocked key wins in this window.
    device = "remote" if device_remote else "keyboard"
    for key in effective_blocks():
        if key in seen or block_device(key) != device:
            continue
        seen.add(key)
        lines.append("      <%s>noop</%s>" % (key, key))
    body = "\n".join(lines)
    return body + "\n" if body else ""


def _typing_body():
    """The single-source hardware-typing key block (all GEN:typing sections)."""
    return "".join("      %s\n" % l for l in TYPING_LINES)


def render(defaults=None, user=None):
    """Fill the template markers. Returns (xml, None) or (None, error)."""
    defaults = _defaults() if defaults is None else defaults
    user = _user() if user is None else user
    if not defaults:
        return None, "no functions in %s" % DEFAULTS_FILE
    template = read_text(os.path.join(skin_root(), "remotes", "template.xml"))
    if not template:
        return None, "unreadable remotes/template.xml"
    out = template
    for name in BLOCKS:
        body = _block_body(name, defaults, user)
        pat = "<!--GEN:%s-->(.*?)<!--/GEN:%s-->" % (name, name)
        out, n = re.subn(pat, lambda m: "<!--GEN:%s-->\n%s      <!--/GEN:%s-->" % (name, body, name),
                         out, flags=re.S)
        if n != 1:
            return None, "marker GEN:%s found %d times" % (name, n)
    # Opt-in mouse-button locks live in <global><mouse> (device-global).
    mouse = [k for k in effective_blocks() if block_device(k) == "mouse"]
    mbody = "\n".join("      <%s>noop</%s>" % (k, k) for k in mouse)
    mbody = mbody + "\n" if mbody else ""
    pat = "<!--GEN:blocks_mouse-->(.*?)<!--/GEN:blocks_mouse-->"
    out, n = re.subn(pat, lambda m: "<!--GEN:blocks_mouse-->\n%s      <!--/GEN:blocks_mouse-->" % mbody,
                     out, flags=re.S)
    if n != 1:
        return None, "marker GEN:blocks_mouse found %d times" % n
    # Hardware-typing block: fill EVERY occurrence (Home + settings window).
    tbody = _typing_body()
    pat = "<!--GEN:typing-->(.*?)<!--/GEN:typing-->"
    out, n = re.subn(pat, lambda m: "<!--GEN:typing-->\n%s      <!--/GEN:typing-->" % tbody,
                     out, flags=re.S)
    if n < 1:
        return None, "marker GEN:typing not found"
    return out, None


def build():
    """Regenerate keymaps/browsybare.xml when the output changed (True when
    rewritten). Never writes a broken file."""
    xml, err = render()
    if err:
        log("remotes: %s, keep keymap" % err)
        return False
    dst = os.path.join(skin_root(), "keymaps", "browsybare.xml")
    if read_text(dst) == xml:
        return False
    try:
        import xml.etree.ElementTree as ET
        ET.fromstring(xml)
    except Exception as e:
        log("remotes: generated XML invalid (%s), keep keymap" % e)
        return False
    try:
        write_text(dst, xml)
    except Exception as e:
        # Read-only addon install: never abort the whole sync for the keymap.
        log("remotes: keymap write failed (%s), keep existing" % e)
        return False
    log("remotes: keymap rebuilt")
    return True


if __name__ == "__main__":
    build()

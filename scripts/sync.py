#!/usr/bin/env python3
"""Base-layer sync + skin setup (branding/accents/keymap).

The only place that touches Estuary base-layer files."""
import hashlib
import os
import platform
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from common import log, ok, fail, skin_name, skin_root, state_dir, read_json, read_text, write_text, write_json, record_issue, drop_issues
import remotes

# ---------------------------------------------------------------- sync

# Top-level locations Estuary must never clobber. changelog.txt is blocked
# although we ship none, so the manager shows no foreign changelog.
PROTECTED_DIRS = {"scripts", "docs", "dev", "changelogs"}
PROTECTED_FILES = {
    "addon.xml", "icon.png", "fanart.jpg",
    "changelog.txt", "README.md", "LICENSE",
}
# Bootstrap stubs that must be Estuary originals after a sync (a zip update
# can re-overwrite them while the marker still says up to date).
STUB_SENTINEL = "BROWSYBARE-STUB"
STUB_GUARD = (
    "xml/Settings.xml",
    "xml/SettingsCategory.xml",
    "xml/Includes.xml",
    "xml/Font.xml",
)


def stubs_pending(root):
    """True when a synced base file regressed to our bootstrap stub (contains
    the sentinel comment)."""
    for rel in STUB_GUARD:
        try:
            with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as f:
                if STUB_SENTINEL in f.read(2000):
                    return True
        except Exception:
            pass
    return False
# xml/: our own windows (never overwritten by Estuary). DialogConfirm is ours
# now (our dark panel); the other core dialogs
# stay Estuary base-layer; DialogBusy is a protected tint-only fork.
OUR_XML = {
    "Home.xml",
    "DialogConfirm.xml",
    "DialogBusy.xml",
    "DialogContextMenu.xml",
    "IncludesPowerMenu.xml",
    "IncludesInfo.xml",
    "VideoOSD.xml",
    "MusicOSD.xml",
    "MusicVisualisation.xml",
    "DialogVolumeBar.xml",
    "DialogSeekBar.xml",
    "DialogExtendedProgressBar.xml",
    "Pointer.xml",
}
OUR_INCLUDES = []
# Include files from earlier versions; their references are removed from the
# synced Includes.xml.
LEGACY_INCLUDE_FILES = [
    "Includes_Theme_Standard.xml",
    "Includes_Theme_Modern.xml",
    "../themes/SquarePants.xml",
    "../themes/RoundPants.xml",
    "IncludesMusicHero.xml",
]
# Layout definition (positions as include params)
OUR_INCLUDES.append("Layout.xml")
OUR_INCLUDES.append("IncludesKeyboard.xml")
OUR_INCLUDES.append("IncludesPowerMenu.xml")
OUR_INCLUDES.append("IncludesInfo.xml")


def estuary_path():
    try:
        addon = xbmcaddon.Addon("skin.estuary")
        return addon.getAddonInfo("path")
    except Exception:
        return None


def estuary_version(est):
    try:
        with open(os.path.join(est, "addon.xml"), "r", encoding="utf-8") as f:
            head = f.read(2000)
        m = re.search(r'<addon[^>]*?\sversion="([^"]+)"', head)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "unknown"


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_files(est):
    files = {}
    for dirpath, dirnames, filenames in os.walk(est):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, est).replace(os.sep, "/")
            try:
                files[rel] = sha1(full)
            except Exception:
                pass
    return files


def should_copy(rel):
    parts = rel.split("/")
    top = parts[0]
    if top in PROTECTED_DIRS:
        return False
    if len(parts) == 1:
        return parts[0] not in PROTECTED_FILES
    if top == "language":
        return False  # merged, not copied
    if top == "xml":
        base = parts[1]
        # never let Estuary overwrite our own windows
        if base in OUR_XML:
            return False
    return True


def parse_po(text):
    """Small .po parser: dict key -> (msgid, msgstr); key is the numeric ID
    from msgctxt "#N", otherwise the msgid text. Non-numeric msgctxt (addon
    metadata) is skipped."""
    entries = {}
    msgid = None
    msgstr = None
    msgctxt = None
    state = None

    def flush():
        nonlocal msgid, msgstr, msgctxt, state
        if msgid is not None:
            m = re.match(r"#(\d+)$", msgctxt or "")
            if m:
                entries[int(m.group(1))] = (msgid, msgstr or "")
            elif not msgctxt and msgid:
                # Plain catalog entry (no context): key by msgid.
                entries[msgid] = (msgid, msgstr or "")
            # else: non-numeric context -> addon metadata, drop it.
        msgid = msgstr = msgctxt = None
        state = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # New entry: a msgid line, or a msgctxt after a completed msgstr.
        if msgid is not None and (line.startswith("msgid ") or
                                  (line.startswith("msgctxt ") and state == "msgstr")):
            flush()
        if line.startswith("msgid "):
            msgid = line[6:].strip().strip('"')
            state = "msgid"
        elif line.startswith("msgctxt "):
            msgctxt = line[8:].strip().strip('"')
            state = "msgctxt"
        elif line.startswith("msgstr "):
            msgstr = line[7:].strip().strip('"')
            state = "msgstr"
        elif line.startswith('"'):
            part = line.strip().strip('"')
            if state == "msgid":
                msgid += part
            elif state == "msgstr":
                msgstr += part
            elif state == "msgctxt":
                msgctxt += part
    flush()
    return entries


def parse_colors(text):
    """<color name="x">HEX</color> entries -> dict name -> HEX (order preserved)."""
    colors = {}
    for m in re.finditer(r'<color name="([^"]+)">([^<]+)</color>', text):
        colors[m.group(1)] = m.group(2).strip()
    return colors


def merge_colors(est, root):
    """Merge our palette (colors/standard.xml) into Estuary defaults.xml
    (skincolors is reset on every skin switch, so scheme files never apply)."""
    est_defaults = os.path.join(est, "colors", "defaults.xml")
    our_palette = os.path.join(root, "colors", "standard.xml")
    dst = os.path.join(root, "colors", "defaults.xml")
    if not os.path.isfile(est_defaults) or not os.path.isfile(our_palette):
        return
    est_txt, our_txt = read_text(est_defaults), read_text(our_palette)
    if est_txt is None or our_txt is None:
        log("merge_colors: unreadable input, skipped")
        return
    merged = parse_colors(est_txt)
    merged.update(parse_colors(our_txt))
    out = '<?xml version="1.0" encoding="UTF-8"?>\n<colors>\n'
    for name, value in merged.items():
        out += '\t<color name="%s">%s</color>\n' % (name, value)
    out += '</colors>\n'
    write_text(dst, out)


def merge_includes(est, root):
    """Append our include file= references to Estuary's Includes.xml (Kodi only
    loads that one file and expands <include file=.../> recursively)."""
    est_inc = os.path.join(est, "xml", "Includes.xml")
    dst = os.path.join(root, "xml", "Includes.xml")
    if not os.path.isfile(est_inc) or not os.path.isfile(dst):
        return
    s = read_text(dst)
    if s is None:
        log("merge_includes: unreadable %s, skipped" % dst)
        return
    for name in LEGACY_INCLUDE_FILES:
        # remove the reference line (including whitespace)
        s = re.sub(r'[ \t]*<include[ \t]+file="%s"[ \t]*/>[ \t]*\n?' % re.escape(name), "", s)
    for name in OUR_INCLUDES:
        if 'file="%s"' % name not in s:
            s = s.replace("</includes>",
                          '\t<include file="%s" />\n</includes>' % name, 1)
    # Replace Estuary's static primary/secondary_background colors with LIVE
    # accent labels so the base background follows the accent without a reload.
    s = s.replace('colordiffuse="primary_background"',
                  'colordiffuse="$INFO[Window(10000).Property(bp.accent.bg)]"')
    s = s.replace('colordiffuse="secondary_background"',
                  'colordiffuse="$INFO[Window(10000).Property(bp.accent.bg2)]"')
    write_text(dst, s)


def merge_focus_color(est, root):
    """Rewrite Estuary's static `button_focus` color to a LIVE accent label
    across the synced XML; idempotent."""
    xmldir = os.path.join(root, "xml")
    if not os.path.isdir(xmldir):
        return
    rep_cd = 'colordiffuse="$INFO[Window(10000).Property(bp.accent.focus)]"'
    rep_tc = '<textcolor>$INFO[Window(10000).Property(bp.accent.focus)]</textcolor>'
    for name in os.listdir(xmldir):
        if not name.endswith(".xml"):
            continue
        p = os.path.join(xmldir, name)
        s = read_text(p)
        if not s or "button_focus" not in s:
            continue
        s2 = s.replace('colordiffuse="button_focus"', rep_cd)
        s2 = s2.replace('<textcolor>button_focus</textcolor>', rep_tc)
        if s2 != s:
            write_text(p, s2)


FONT_CAPTION = "font_caption"
FONT_TAB = "font_tab"
FONT_MODAL = "font25_modal"
# Stable arial for the install veil header so it does not change mid-install
# when the veil survives the post-sync reload.
FONT_VEIL = "font_veil"


def merge_fonts(est, root):
    """Inject our custom fonts (font_caption, font_tab, font_veil,
    font25_modal) into the copied Estuary Font.xml; idempotent, added to every
    <fontset>."""
    dst = os.path.join(root, "xml", "Font.xml")
    s = read_text(dst)
    if s is None:
        return
    if "</fontset>" not in s:
        log("merge_fonts: no fontset in %s, skipped" % dst)
        return
    changed = False
    if "<name>%s</name>" % FONT_CAPTION not in s:
        block = ('\t\t<font>\n'
                 '\t\t\t<name>%s</name>\n'
                 '\t\t\t<filename>arial.ttf</filename>\n'
                 '\t\t\t<size>16</size>\n'
                 '\t\t</font>\n' % FONT_CAPTION)
        s = s.replace("</fontset>", block + "\t</fontset>")
        log("merge_fonts: added %s" % FONT_CAPTION)
        changed = True
    if "<name>%s</name>" % FONT_TAB not in s:
        block = ('\t\t<font>\n'
                 '\t\t\t<name>%s</name>\n'
                 '\t\t\t<filename>arial.ttf</filename>\n'
                 '\t\t\t<size>20</size>\n'
                 '\t\t</font>\n' % FONT_TAB)
        s = s.replace("</fontset>", block + "\t</fontset>")
        log("merge_fonts: added %s" % FONT_TAB)
        changed = True
    if "<name>%s</name>" % FONT_VEIL not in s:
        block = ('\t\t<font>\n'
                 '\t\t\t<name>%s</name>\n'
                 '\t\t\t<filename>arial.ttf</filename>\n'
                 '\t\t\t<size>25</size>\n'
                 '\t\t</font>\n' % FONT_VEIL)
        s = s.replace("</fontset>", block + "\t</fontset>")
        log("merge_fonts: added %s" % FONT_VEIL)
        changed = True
    if "<name>%s</name>" % FONT_MODAL not in s:
        def _clone(m):
            blk = m.group(0)
            fn = re.search(r"<filename>([^<]+)</filename>", blk)
            sz = re.search(r"<size>(\d+)</size>", blk)
            if not fn or not sz:
                return blk
            indent = re.match(r"[ \t]*", blk).group(0)
            sep = "" if blk.endswith("\n") else "\n"
            block = ('%s<font>\n'
                     '%s\t<name>%s</name>\n'
                     '%s\t<filename>%s</filename>\n'
                     '%s\t<size>%s</size>\n'
                     '%s</font>' % (indent, indent, FONT_MODAL, indent, fn.group(1),
                                    indent, sz.group(1), indent))
            return blk + sep + block
        s2 = re.sub(r"[ \t]*<font>\s*<name>font25_title</name>.*?</font>", _clone, s, flags=re.S)
        if s2 != s:
            s = s2
            log("merge_fonts: added %s" % FONT_MODAL)
            changed = True
    if changed:
        write_text(dst, s)


POWER_MENU = "IncludesPowerMenu.xml"
POWER_ROW_H = 60
POWER_ROW_GAP = 16
POWER_PANEL_W = 704
POWER_LIST_W = 664
POWER_PANEL_LEFT = 608
POWER_TOP0 = 80
# Destructive OS actions get a specific confirm question; everything else gets
# the generic confirm.
POWER_CONFIRM = ("Quit", "Powerdown", "Reset", "Suspend", "Hibernate")
# Shutdown-timer rows open a core input dialog our remote cannot drive -> skipped.
POWER_SKIP = ("AlarmClock", "CancelAlarm")
# Fallback label for our shutdown-timer row when Estuary has no timer row.
TIMER_LABEL = "$LOCALIZE[31405]"

# Explicit buttons in a plain group (a grouplist inside a toggled overlay lost
# focus on Kodi 21).
_POWER_ROW = """\t\t\t\t<control type="button" id="{cid}">
\t\t\t\t\t<left>20</left><top>{top}</top><width>{lw}</width><height>{rh}</height>
\t\t\t\t\t<texturefocus border="18" colordiffuse="$INFO[Window(10000).Property(bp.accent.hov)]">drawn/row.png</texturefocus>
\t\t\t\t\t<texturenofocus border="18" colordiffuse="FF18181E">drawn/row.png</texturenofocus>
\t\t\t\t\t<font>font13</font><align>center</align><aligny>center</aligny>
\t\t\t\t\t<textcolor>FF9A9AA2</textcolor>
\t\t\t\t\t<focusedcolor>$INFO[Skin.String(accent)]</focusedcolor>
\t\t\t\t\t<label>{label}</label>
{actions}\t\t\t\t\t<onup>{up}</onup>
\t\t\t\t\t<ondown>{down}</ondown>
\t\t\t\t\t<onleft>{cid}</onleft>
\t\t\t\t\t<onright>{cid}</onright>
\t\t\t\t\t<onback>ClearProperty(bp.power)</onback>
\t\t\t\t\t<onback>SetFocus(33)</onback>
\t\t\t\t</control>
"""

_POWER_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!-- Browsybare shutdown menu: GENERATED by scripts/sync.py from the installed
     Estuary's DialogButtonMenu.xml. The action ROWS (label/onclick, filtered to
     the rows VISIBLE on this platform) are copied from Estuary; the look and
     navigation are OUR Home overlay. Do not edit; edit the generator. -->
<includes>
\t<include name="PowerMenu">
\t\t<control type="group">
\t\t\t<visible>String.IsEqual(Window(10000).Property(bp.power),open)</visible>
\t\t\t<animation effect="fade" start="0" end="100" time="120">Visible</animation>
\t\t\t<animation effect="slide" start="0,24" end="0,0" time="150" tween="cubic" easing="out">Visible</animation>
\t\t\t<control type="button" id="952">
\t\t\t\t<left>0</left><top>0</top><width>1920</width><height>1080</height>
\t\t\t\t<texturefocus colordiffuse="80101014">drawn/fill.png</texturefocus>
\t\t\t\t<texturenofocus colordiffuse="80101014">drawn/fill.png</texturenofocus>
\t\t\t\t<onclick>ClearProperty(bp.power)</onclick>
\t\t\t\t<onclick>SetFocus(33)</onclick>
\t\t\t\t<onback>ClearProperty(bp.power)</onback>
\t\t\t\t<onback>SetFocus(33)</onback>
\t\t\t\t<onleft>952</onleft>
\t\t\t\t<onright>952</onright>
\t\t\t\t<onup>952</onup>
\t\t\t\t<ondown>952</ondown>
\t\t\t</control>
\t\t\t<control type="group">
\t\t\t\t<left>{left}</left><top>{top}</top>
\t\t\t\t<control type="image">
\t\t\t\t\t<left>0</left><top>0</top><width>{pw}</width><height>{total_h}</height>
\t\t\t\t\t<texture border="24" colordiffuse="FF1E1E26">drawn/panel.png</texture>
\t\t\t\t</control>
\t\t\t\t<control type="button" id="953">
\t\t\t\t\t<left>0</left><top>0</top><width>{pw}</width><height>{total_h}</height>
\t\t\t\t\t<texturefocus /><texturenofocus />
\t\t\t\t\t<onclick>noop</onclick>
\t\t\t\t\t<onback>ClearProperty(bp.power)</onback>
\t\t\t\t\t<onback>SetFocus(33)</onback>
\t\t\t\t\t<onleft>{first_id}</onleft>
\t\t\t\t\t<onright>{first_id}</onright>
\t\t\t\t\t<onup>{first_id}</onup>
\t\t\t\t\t<ondown>{first_id}</ondown>
\t\t\t\t</control>
\t\t\t\t<control type="image">
\t\t\t\t\t<left>0</left><top>0</top><width>{pw}</width><height>60</height>
\t\t\t\t\t<texture border="24" colordiffuse="FF32323C">drawn/header.png</texture>
\t\t\t\t</control>
\t\t\t\t<control type="label">
\t\t\t\t\t<left>0</left><top>0</top><width>{pw}</width><height>60</height>
\t\t\t\t\t<font>font25_modal</font><textcolor>FFF2F2F4</textcolor>
\t\t\t\t\t<align>center</align><aligny>center</aligny>
\t\t\t\t\t<label>$LOCALIZE[31310]</label>
\t\t\t\t</control>
{rows}\t\t\t\t<control type="button" id="951">
\t\t\t\t\t<left>20</left><top>{cancel_top}</top><width>{lw}</width><height>{rh}</height>
\t\t\t\t\t<texturefocus border="18" colordiffuse="$INFO[Window(10000).Property(bp.accent.hov)]">drawn/row.png</texturefocus>
\t\t\t\t\t<texturenofocus border="18" colordiffuse="FF18181E">drawn/row.png</texturenofocus>
\t\t\t\t\t<font>font13</font><align>center</align><aligny>center</aligny>
\t\t\t\t\t<textcolor>FF9A9AA2</textcolor>
\t\t\t\t\t<focusedcolor>$INFO[Skin.String(accent)]</focusedcolor>
\t\t\t\t\t<label>$LOCALIZE[31344]</label>
\t\t\t\t\t<onclick>ClearProperty(bp.power)</onclick>
\t\t\t\t\t<onclick>SetFocus(33)</onclick>
					<onup>{last_id}</onup>
					<ondown>{first_id}</ondown>
					<onleft>951</onleft>
					<onright>951</onright>
\t\t\t\t\t<onback>ClearProperty(bp.power)</onback>
\t\t\t\t\t<onback>SetFocus(33)</onback>
\t\t\t\t</control>
\t\t\t</control>
\t\t</control>
\t</include>
</includes>
"""


def _power_actions(label, onclicks):
    """Row onclicks from Estuary -> ours: specific confirm for destructive ops,
    generic otherwise. Dialog.Close(shutdownmenu) is dropped."""
    ops = [o.strip() for o in onclicks if o and o.strip()]
    simple = [o for o in ops if o != "Dialog.Close(shutdownmenu)"]
    if len(simple) == 1:
        mm = re.match(r"^([A-Za-z_]+)\(\)$", simple[0])
        if mm:
            op = mm.group(1)
            if op in POWER_CONFIRM:
                return ["SetProperty(bp.confirm.title,%s)" % label,
                        "SetProperty(bp.confirm.op,%s)" % op,
                        "RunScript(special://skin/scripts/main.py,powerconfirm)"]
    # cmds carries the exact count so stale higher indices cannot be replayed.
    out = ["SetProperty(bp.confirm.title,%s)" % label,
           "SetProperty(bp.confirm.cmds,%d)" % len(simple)]
    for i, c in enumerate(simple, 1):
        c = c.replace("dialog.close(all,true)", "Dialog.Close(all)")
        out.append("SetProperty(bp.confirm.cmd.%d,%s)" % (i, c))
    out.append("RunScript(special://skin/scripts/main.py,powergeneric)")
    return out


def merge_button_menu(est, root):
    """Generate our Home overlay PowerMenu from Estuary's DialogButtonMenu rows
    (only rows visible on this platform). Returns True when it rewrote the
    file."""
    src = os.path.join(est, "xml", "DialogButtonMenu.xml")
    txt = read_text(src)
    if txt is None:
        return False
    m = re.search(r"<content>(.*?)</content>", txt, re.S)
    if not m:
        log("merge_button_menu: no <content> in Estuary DialogButtonMenu.xml, skipped")
        return False
    try:
        node = ET.fromstring("<content>%s</content>" % m.group(1))
    except Exception as e:
        log("merge_button_menu: content parse failed (%s), skipped" % e)
        return False
    items = []
    timer_at = None
    timer_label = None
    for item in node.findall("item"):
        labels = [(x.text or "").strip() for x in item.findall("label")]
        label = labels[0] if labels else ""
        onclicks = [(o.text or "").strip() for o in item.findall("onclick")]
        visibles = [(v.text or "").strip() for v in item.findall("visible") if (v.text or "").strip()]
        if any(any(k in o for k in POWER_SKIP) for o in onclicks):
            # Remember Estuary's timer row position and label (the Beenden
            # menu keeps its texts).
            if timer_at is None and any("AlarmClock" in o for o in onclicks):
                timer_at = len(items)
                timer_label = label
            continue
        if visibles and not all(xbmc.getCondVisibility(c) for c in visibles):
            continue
        items.append((label, _power_actions(label, onclicks)))
    if xbmc.getCondVisibility("System.CanPowerDown"):
        row = (timer_label or TIMER_LABEL, ["RunScript(special://skin/scripts/main.py,timeropen)"])
        items.insert(timer_at if timer_at is not None else len(items), row)
    n = len(items)
    step = POWER_ROW_H + POWER_ROW_GAP
    ids = [960 + i for i in range(n)]
    first_id = ids[0] if ids else 951
    rows = []
    for i, (label, acts) in enumerate(items):
        # Wrap-around: first/last rows and Cancel navigate to each other.
        up = ids[i - 1] if i > 0 else 951
        down = ids[i + 1] if i + 1 < n else 951
        actions = "".join("\t\t\t\t\t<onclick>%s</onclick>\n" % a for a in acts)
        rows.append(_POWER_ROW.format(cid=ids[i], top=POWER_TOP0 + i * step,
                                      lw=POWER_LIST_W, rh=POWER_ROW_H, label=label,
                                      actions=actions, up=up, down=down))
    cancel_top = POWER_TOP0 + n * step
    total_h = cancel_top + POWER_ROW_H + 20
    top = max(0, (1080 - total_h) // 2)
    out = _POWER_TEMPLATE.format(left=POWER_PANEL_LEFT, top=top, pw=POWER_PANEL_W,
                                 total_h=total_h, rows="".join(rows),
                                 cancel_top=cancel_top, lw=POWER_LIST_W,
                                 rh=POWER_ROW_H, last_id=(ids[-1] if ids else 960),
                                 first_id=first_id)
    dst = os.path.join(root, "xml", POWER_MENU)
    if read_text(dst) == out:
        return False
    write_text(dst, out)
    # Flag boot to force one skin reload (the include was parsed before this
    # ran); idempotent.
    try:
        xbmcgui.Window(10000).setProperty("bp.powermenu.changed", "1")
    except Exception:
        pass
    log("merge_button_menu: wrote %s (%d rows)" % (POWER_MENU, n))
    return True


def merge_language(est, root):
    """Merge Estuary languages into the skin: union by ID, our strings win.
    Numeric IDs keep their msgctxt line; retired range 31000-31299 is restored
    to Estuary."""

    # Our IDs must stay clear of Estuary's 31000-31611.
    RETIRED_LO, RETIRED_HI = 31000, 31299
    est_lang = os.path.join(est, "language")
    root_lang = os.path.join(root, "language")
    if not os.path.isdir(est_lang):
        return
    for lang in sorted(os.listdir(est_lang)):
        src = os.path.join(est_lang, lang, "strings.po")
        if not os.path.isfile(src):
            continue
        dst_dir = os.path.join(root_lang, lang)
        dst = os.path.join(dst_dir, "strings.po")
        est_txt = read_text(src)
        if est_txt is None:
            log("merge_language: unreadable source, skipped: %s" % src)
            continue
        est_entries = parse_po(est_txt)
        dst_dir = os.path.join(root_lang, lang)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "strings.po")
        our_txt = read_text(dst)
        if our_txt:
            # self-healing: fall back to Estuary entries on unreadable file
            merged = dict(est_entries)
            merged.update(parse_po(our_txt))
            header = our_txt.split('\n\n', 1)[0]
        else:
            merged = dict(est_entries)
            header = est_txt.split('\n\n', 1)[0]
        # Retired range: Estuary always wins (see docstring).
        for key in [k for k in merged if isinstance(k, int) and RETIRED_LO <= k <= RETIRED_HI]:
            if key in est_entries:
                merged[key] = est_entries[key]
            else:
                del merged[key]
        out = header + "\n\n"
        for key in sorted(merged, key=lambda k: (0, k) if isinstance(k, int) else (1, k)):
            mid, mstr = merged[key]
            if isinstance(key, int):
                out += 'msgid "%s"\nmsgctxt "#%d"\nmsgstr "%s"\n\n' % (mid, key, mstr)
            else:
                out += 'msgid "%s"\nmsgstr "%s"\n\n' % (mid, mstr)
        write_text(dst, out)


def keymaps():
    """Copy our hardware guard keymap to userdata/keymaps so Kodi loads it."""
    try:
        src = os.path.join(skin_root(), "keymaps", "browsybare.xml")
        if not os.path.isfile(src):
            return
        dst_dir = xbmcvfs.translatePath("special://userdata/keymaps")
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "browsybare.xml")
        # copy if changed
        if os.path.isfile(dst) and sha1(src) == sha1(dst):
            return
        shutil.copy2(src, dst)
        log("keymaps: browsybare.xml -> %s" % dst)
        # reload so the new keyboard capture is active
        try:
            xbmc.executebuiltin("ReloadKeymaps")
        except Exception:
            pass
        try:
            xbmc.executebuiltin("Action(reloadkeymaps)")
        except Exception:
            pass
    except Exception as e:
        log("keymaps error: %s" % e)

def clean_wrongcase_language_dirs(root):
    """Remove misnamed language folders with uppercase regions (breaks
    $LOCALIZE on case-sensitive filesystems). Only deletes when the wrong-case
    name is a distinct directory entry."""
    lang_dir = os.path.join(root, "language")
    try:
        entries = os.listdir(lang_dir)
    except Exception:
        return
    for wrong, right in (("resource.language.de_DE", "resource.language.de_de"),
                         ("resource.language.en_GB", "resource.language.en_gb")):
        if wrong in entries and right in entries:
            try:
                shutil.rmtree(os.path.join(lang_dir, wrong))
                log("language: removed misnamed folder %s" % wrong)
            except Exception as e:
                log("language cleanup error: %s" % e)


def _set_sync_bar(win, pct):
    """Drive the Home sync veil's 20-segment bar (one boolean property per
    segment, only changed ones written)."""
    try:
        pct = max(0, min(100, int(pct)))
    except (TypeError, ValueError):
        pct = 0
    filled = (pct * 20) // 100
    for i in range(1, 21):
        k = "bp.sync.f%d" % i
        if i <= filled:
            win.setProperty(k, "1")
        else:
            win.clearProperty(k)


def skin_version(root=None):
    """Own addon version from addon.xml (reinstall/downgrade watch)."""
    try:
        with open(os.path.join(root or skin_root(), "addon.xml"), "r", encoding="utf-8") as f:
            head = f.read(2000)
        m = re.search(r'<addon[^>]*?\sversion="([^"]+)"', head)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "?"


def sync():
    """Returns True when a full base copy ran (caller reloads the skin once),
    else False."""
    branding()
    remotes.build()
    keymaps()
    root = skin_root()
    est = estuary_path()
    if not est or not os.path.isdir(est):
        fail("Estuary not found (skin.estuary). Please install Estuary and restart the skin.")
        return False
    clean_wrongcase_language_dirs(root)
    # Transient progress flags: stale values from a killed session must not stick.
    try:
        xbmcgui.Window(10000).clearProperty("bp.sync.active")
        xbmcgui.Window(10000).clearProperty("bp.sync.t0")
        xbmcgui.Window(10000).clearProperty("bp.sync.phase")
    except Exception:
        pass

    # Fresh bootstrap install: Variables.xml is Estuary-only, so its absence
    # means the base layer is gone.

    kodi_version = xbmc.getInfoLabel("System.BuildVersion")
    est_version = estuary_version(est)
    current = collect_files(est)

    marker_file = os.path.join(state_dir(), ".sync.json")
    marker = read_json(marker_file, None)
    # Variables.xml is Estuary-only; its absence means a zip update wiped the
    # base -- re-copy despite an up-to-date marker.
    base_missing = not os.path.isfile(os.path.join(root, "xml", "Variables.xml"))
    wipe_copy = False
    if marker and marker.get("kodi") == kodi_version and marker.get("files") == current:
        if base_missing:
            log("sync: base layer missing (update wipe?), re-syncing base layer")
            wipe_copy = True
        elif stubs_pending(root):
            log("sync: bootstrap stubs present, re-syncing base layer")
            wipe_copy = True
        else:
            log("sync: up to date (Kodi %s, Estuary %s)" % (kodi_version, est_version))
            merge_language(est, root)
            merge_colors(est, root)
            merge_includes(est, root)
            merge_focus_color(est, root)
            merge_fonts(est, root)
            merge_button_menu(est, root)
            ok()
            return False
    if wipe_copy:
        # Note for boot.py's reinstall watch: this copy was triggered by a
        # wiped addon folder.
        try:
            write_json(os.path.join(state_dir(), "version-reinstall.json"),
                       {"pending": True})
        except Exception:
            pass

    copied = 0
    failed = 0
    total = sum(1 for rel in current if should_copy(rel))
    done = 0
    win = xbmcgui.Window(10000)
    win.setProperty("bp.sync.active", "1")
    win.setProperty("bp.sync.phase", "copy")
    _set_sync_bar(win, 0)
    # Hide the UI while the base copies (parsed windows go stale until the
    # reload); boot.py sets bp.ready once the UI is final.
    try:
        win.clearProperty("bp.ready")
    except Exception:
        pass
    try:
        win.setProperty("bp.sync.t0", str(time.time()))
    except Exception:
        pass
    # Park focus on the loading veil (button 29) so the half-built stubs take
    # no input, but never steal it from the open skin-keep dialog (10100).
    try:
        if not xbmc.getCondVisibility("Window.IsActive(10100)"):
            xbmc.executebuiltin("SetFocus(29)")
    except Exception:
        pass
    for rel in sorted(current):
        if not should_copy(rel):
            continue
        src = os.path.join(est, *rel.split("/"))
        dst = os.path.join(root, *rel.split("/"))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except Exception as e:
            # One unreadable file must never kill the whole base layer.
            failed += 1
            log("sync: copy failed, skipped: %s (%s)" % (rel, e))
        done += 1
        if total and done % 10 == 0:
            _set_sync_bar(win, int(100 * done / total))

    _set_sync_bar(win, 100)
    win.setProperty("bp.sync.phase", "wait")  # copy done -> animated wait phase
    merge_language(est, root)
    merge_colors(est, root)
    merge_includes(est, root)
    merge_focus_color(est, root)
    merge_fonts(est, root)
    merge_button_menu(est, root)
    if failed:
        # A copy failure must not be marked up to date (the marker carries the
        # source hash map), so leave it stale and retry next boot.
        log("sync: %d files copied, %d FAILED -- marker NOT written, retry next boot"
            % (copied, failed))
        record_issue("copy_failed", count=failed)
        ok()
        return True
    drop_issues("copy_failed")
    write_json(marker_file, {
        "kodi": kodi_version,
        "estuary": est_version,
        "files": current,
    })
    log("sync: %d files (Kodi %s, Estuary %s)" % (copied, kodi_version, est_version))
    ok()
    return True


# ---------------------------------------------------------------- branding

def sys_os_line():
    """OS name for the about footer, mirroring Kodi's GetOsPrettyName (pure
    python: the System.OSVersionInfo label is racy)."""
    if sys.platform == "darwin":
        return "macOS %s" % (platform.mac_ver()[0] or "?")
    if sys.platform == "win32":
        return "Windows %s" % (platform.win32_ver()[1] or "?")
    if sys.platform.startswith("linux"):
        try:
            import sources as _sources
            android = _sources._is_android()
        except Exception:
            android = False
        if android:
            props = {}
            try:
                with open("/system/build.prop", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "=" in line:
                            k, v = line.split("=", 1)
                            props[k.strip()] = v.strip()
            except Exception:
                pass
            if props.get("ro.product.manufacturer", "").lower() == "amazon":
                # Fire OS maps the underlying Android release
                # (6 -> 7.1, 7 -> 9, 8 -> 11).
                generation = {"7.1": "6", "9": "7", "11": "8"}.get(
                    props.get("ro.build.version.release", ""), "")
                return ("Fire OS %s" % generation).strip()
            return ("Android %s" % props.get("ro.build.version.release", "")).strip()
        vals = {}
        try:
            with open("/etc/os-release", encoding="utf-8") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        vals[k] = v.strip().strip('"')
        except Exception:
            pass
        return (vals.get("NAME", "Linux") + " " + vals.get("VERSION_ID", "")).strip()
    return sys.platform


def _macos_refresh_rate():
    """Main display refresh rate (Hz, rounded) via CoreGraphics + ctypes, for
    windowed mode where the System.ScreenResolution label drops the Hz."""
    try:
        import ctypes
        cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayCopyDisplayMode.restype = ctypes.c_void_p
        cg.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
        cg.CGDisplayModeGetRefreshRate.restype = ctypes.c_double
        cg.CGDisplayModeGetRefreshRate.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        mode = cg.CGDisplayCopyDisplayMode(cg.CGMainDisplayID())
        if not mode:
            return ""
        hz = cg.CGDisplayModeGetRefreshRate(mode)
        cf.CFRelease(mode)
        return "%d" % round(hz) if hz > 0 else ""
    except Exception:
        return ""


def sys_refresh_rate():
    """Screen refresh rate in Hz for the about footer (fullscreen label, macOS
    CoreGraphics fallback), else ''."""
    try:
        m = re.search(r"@ ([\d.]+) Hz", xbmc.getInfoLabel("System.ScreenResolution"))
        if m:
            return "%d" % round(float(m.group(1)))
    except Exception:
        pass
    if sys.platform == "darwin":
        return _macos_refresh_rate()
    return ""


def branding():
    """Branding -> window properties for the settings/about overlays."""
    version = ""
    summaries = {}
    descriptions = {}
    try:
        with open(os.path.join(skin_root(), "addon.xml"), encoding="utf-8") as f:
            xml = f.read()
        m = re.search(r'<addon[^>]*?\sversion="([^"]+)"', xml)
        if m:
            version = m.group(1)
        summaries = dict(re.findall(r'<summary\s+lang="([^"]+)">(.*?)</summary>', xml, re.S))
        descriptions = dict(re.findall(r'<description\s+lang="([^"]+)">(.*?)</description>', xml, re.S))
    except Exception:
        pass
    # Parse addon.xml directly (not xbmcaddon.getAddonInfo, which serves
    # cached metadata missing newly added languages).
    summary = description = ""
    try:
        try:
            gui = (xbmc.getLanguage(xbmc.ISO_639_1) or "").lower()
        except Exception:
            gui = ""
        cands = [k for k in summaries if k.lower().startswith(gui)] if gui else []
        cands += [k for k in summaries if k.lower().startswith("en")]
        cands += list(summaries)
        if cands:
            summary = summaries[cands[0]]
        cands = [k for k in descriptions if k.lower().startswith(gui)] if gui else []
        cands += [k for k in descriptions if k.lower().startswith("en")]
        cands += list(descriptions)
        if cands:
            description = descriptions[cands[0]]
    except Exception:
        pass
    win = xbmcgui.Window(10000)
    win.setProperty("bp.name", skin_name())
    win.setProperty("bp.version", version)
    win.setProperty("bp.summary", summary)
    win.setProperty("bp.description", description)
    # About footer: OS + refresh rate as properties.
    win.setProperty("bp.sys.os", sys_os_line())
    win.setProperty("bp.sys.hz", sys_refresh_rate())
    log("branding: %s %s" % (skin_name(), version))


# ---------------------------------------------------------------- accents

MAX_ACCENT_SLOTS = 9  # fixed slot geometry in Layout.xml (swatches 110-118)
ACCENT_LEVELS = 5  # intensity steps L1 (soft) .. L5 (vivid originals)
ACCENT_DEFAULT_LEVEL = "L3"


def _valid_level_row(row):
    return (isinstance(row, list) and len(row) == MAX_ACCENT_SLOTS
            and all(isinstance(c, str) and re.fullmatch(r"[0-9a-fA-F]{8}", c) for c in row))


def accent_levels():
    """data/accents.json -> 5 x 9 colors (L1 soft .. L5 vivid); a legacy flat
    9-list is replicated to all levels."""
    data = read_json(os.path.join(skin_root(), "data", "accents.json"), [])
    if isinstance(data, dict) and isinstance(data.get("levels"), list) \
            and len(data["levels"]) == ACCENT_LEVELS \
            and all(_valid_level_row(r) for r in data["levels"]):
        return [[c.upper() for c in row] for row in data["levels"]]
    flat = []
    if isinstance(data, list):
        for c in data:
            if isinstance(c, str) and re.fullmatch(r"[0-9a-fA-F]{8}", c):
                flat.append(c.upper())
    if len(flat) == MAX_ACCENT_SLOTS:
        return [list(flat) for _ in range(ACCENT_LEVELS)]
    return []


def accent_level_name(win):
    """Intensity level L1..L5 from bp.accent.level (the L-prefix avoids the
    integer-localize trap)."""
    try:
        name = (win.getProperty("bp.accent.level") or "").upper()
    except Exception:
        name = ""
    return name if name in ("L1", "L2", "L3", "L4", "L5") else ACCENT_DEFAULT_LEVEL


def set_accent_level(name):
    """Apply intensity level: persist Skin.String + property, move the active
    accent to its new shade, refresh all slots."""
    name = (name or "").upper()
    if name not in ("L1", "L2", "L3", "L4", "L5"):
        return False
    win = xbmcgui.Window(10000)
    levels = accent_levels()
    if not levels:
        return False
    try:
        slot = int(win.getProperty("bp.accent.slot") or "1")
    except (TypeError, ValueError):
        slot = 1
    slot = min(max(slot, 1), MAX_ACCENT_SLOTS)
    new_accent = levels[int(name[1]) - 1][slot - 1]
    try:
        xbmc.executebuiltin("Skin.SetString(accent.level,%s)" % name)
        xbmc.executebuiltin("Skin.SetString(accent,%s)" % new_accent)
    except Exception:
        return False
    win.setProperty("bp.accent.level", name)
    win.setProperty("bp.accent.value", new_accent)
    accents()
    log("accent intensity %s (slot %d, %s)" % (name, slot, new_accent))
    return True


def accents():
    """data/accents.json -> bp.accent.1-9 swatch colors + bp.accent.slot.

    Entries must be 8-digit AARRGGBB hex (pure-label color trap). Also derives
    bp.accent.rgb.1-9 and bp.accent.tint as full color values (a colordiffuse
    label must be the entire value)."""
    levels = accent_levels()
    win = xbmcgui.Window(10000)
    level = accent_level_name(win)
    colors = levels[int(level[1]) - 1] if levels else []
    accent = win.getProperty("bp.accent.value").upper()
    slot = ""
    tint = ""
    if re.fullmatch(r"[0-9a-fA-F]{8}", accent):
        tint = "1A" + accent[2:]
    for i, c in enumerate(colors[:MAX_ACCENT_SLOTS], 1):
        if c == accent:
            slot = str(i)
    if not slot and re.fullmatch(r"[0-9a-fA-F]{8}", accent):
        # Foreign value: mark the nearest slot so the swatch ring still shows.
        ar = [int(accent[k:k+2], 16) for k in (2, 4, 6)]
        best, best_d = "", None
        for i, c in enumerate(colors[:MAX_ACCENT_SLOTS], 1):
            cr = [int(c[k:k+2], 16) for k in (2, 4, 6)]
            d = sum((a - b) ** 2 for a, b in zip(ar, cr))
            if best_d is None or d < best_d:
                best, best_d = str(i), d
        slot = best
    for i in range(1, MAX_ACCENT_SLOTS + 1):
        if i <= len(colors):
            win.setProperty("bp.accent.%d" % i, colors[i - 1])
            win.setProperty("bp.accent.rgb.%d" % i, colors[i - 1][2:])
        else:
            win.setProperty("bp.accent.%d" % i, "")
            win.clearProperty("bp.accent.rgb.%d" % i)
    if slot:
        win.setProperty("bp.accent.slot", slot)
    else:
        win.clearProperty("bp.accent.slot")
    if tint:
        win.setProperty("bp.accent.tint", tint)
    else:
        win.clearProperty("bp.accent.tint")
    # Opaque hover blend: 10% accent baked over the panel tone FF1E1E26 (a
    # transparent tint over dark pills rendered darker).
    base = (0x1E, 0x1E, 0x26)
    blend = "FF%02X%02X%02X" % tuple(
        round(0.1 * int(accent[k:k+2], 16) + 0.9 * b) for k, b in zip((2, 4, 6), base)) \
        if re.fullmatch(r"[0-9a-fA-F]{8}", accent) else ""
    for i in range(1, MAX_ACCENT_SLOTS + 1):
        if i <= len(colors):
            rgb = [int(colors[i - 1][k:k+2], 16) for k in (2, 4, 6)]
            hov = "FF%02X%02X%02X" % tuple(round(0.1 * v + 0.9 * b) for v, b in zip(rgb, base))
            win.setProperty("bp.accent.hov.%d" % i, hov)
            # Per-slot base-layer values: the swatch onclicks copy these so an
            # accent change updates windows even behind a dialog.
            win.setProperty("bp.accent.bg.%d" % i,
                            "FF%02X%02X%02X" % (int(rgb[0] * 0.5), int(rgb[1] * 0.5), int(rgb[2] * 0.5)))
            win.setProperty("bp.accent.bg2.%d" % i, "33%02X%02X%02X" % tuple(rgb))
            win.setProperty("bp.accent.focus.%d" % i, colors[i - 1])
        else:
            win.clearProperty("bp.accent.hov.%d" % i)
            win.clearProperty("bp.accent.bg.%d" % i)
            win.clearProperty("bp.accent.bg2.%d" % i)
            win.clearProperty("bp.accent.focus.%d" % i)
    if blend:
        win.setProperty("bp.accent.hov", blend)
    else:
        win.clearProperty("bp.accent.hov")
    # Accent-tinted base-layer background labels read by merge_includes;
    # bg = darkened accent, bg2 = accent at low alpha.
    if re.fullmatch(r"[0-9a-fA-F]{8}", accent):
        ar, ag, ab = (int(accent[k:k + 2], 16) for k in (2, 4, 6))
        win.setProperty("bp.accent.bg",
                        "FF%02X%02X%02X" % (int(ar * 0.5), int(ag * 0.5), int(ab * 0.5)))
        win.setProperty("bp.accent.bg2", "33%02X%02X%02X" % (ar, ag, ab))
        # Live focus color read by merge_focus_color.
        win.setProperty("bp.accent.focus", accent)
    else:
        win.clearProperty("bp.accent.bg")
        win.clearProperty("bp.accent.bg2")
        win.clearProperty("bp.accent.focus")
    win.clearProperty("bp.accent.value")
    log("accents: %d colors, level %s, slot=%r" % (len(colors), level, slot))

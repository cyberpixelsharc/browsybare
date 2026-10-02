#!/usr/bin/env python3
"""Minimal media metadata reader (pure Python, no dependencies).

read_info(path) returns tags + stream info; covers(path) extracts embedded art."""
import os
import re
import struct

_TEXT_TAGS = {
    b"\xa9nam": "title",
    b"\xa9ART": "artist",
    b"\xa9alb": "album",
    b"\xa9gen": "genre",
    b"\xa9wrt": "composer",
    b"\xa9too": "encoder",
    b"desc": "description",
}

_CODECS = {
    b"avc1": "h264", b"avc3": "h264", b"hvc1": "hevc", b"hev1": "hevc",
    b"av01": "av1", b"vp09": "vp9", b"mp4v": "mpeg4",
    b"mp4a": "aac", b"ac-3": "ac3", b"ec-3": "eac3", b"alac": "alac",
    b"Opus": "opus", b"flac": "flac", b"twos": "pcm", b"sowt": "pcm",
}


def _u16(b, o=0):
    return struct.unpack_from(">H", b, o)[0]


def _u32(b, o=0):
    return struct.unpack_from(">I", b, o)[0]


def _u64(b, o=0):
    return struct.unpack_from(">Q", b, o)[0]


def _le16(b, o=0):
    return struct.unpack_from("<H", b, o)[0]


def _le32(b, o=0):
    return struct.unpack_from("<I", b, o)[0]


def _lang(v):
    """15-bit packed ISO-639-2/T language code -> 'de'/'en'/... ('' for und)."""
    if not v:
        return ""
    s = "".join(chr(((v >> sh) & 0x1F) + 0x60) for sh in (10, 5, 0))
    return "" if s == "und" else s


def _box_header(f, end):
    """Read one box header at the current position -> (type, body, box_end) or None."""
    pos = f.tell()
    if pos + 8 > end:
        return None
    hdr = f.read(8)
    if len(hdr) < 8:
        return None
    size = _u32(hdr)
    typ = hdr[4:8]
    body = pos + 8
    if size == 1:  # 64-bit extended size
        ext = f.read(8)
        if len(ext) < 8:
            return None
        size = _u64(ext)
        body = pos + 16
    elif size == 0:  # box extends to the end
        size = end - pos
    if size < body - pos or pos + size > end:
        return None
    return typ, body, pos + size


def _find(f, start, end, name):
    """Find a box by type in [start, end) -> (body, end) or None."""
    f.seek(start)
    while f.tell() + 8 <= end:
        r = _box_header(f, end)
        if not r:
            return None
        if r[0] == name:
            return r[1], r[2]
        f.seek(r[2])
    return None


def _children(f, start, end):
    f.seek(start)
    out = []
    while f.tell() + 8 <= end:
        r = _box_header(f, end)
        if not r:
            break
        out.append(r)
        f.seek(r[2])
    return out


def _text(f, start, end):
    r = _find(f, start, end, b"data")
    if not r:
        return ""
    f.seek(r[0])
    raw = f.read(r[1] - r[0])
    if len(raw) < 8:  # 4 bytes type + 4 bytes locale, then the value
        return ""
    return raw[8:].decode("utf-8", "replace").strip("\x00").strip()


def _year(s):
    m = re.search(r"\d{4}", s or "")
    return m.group(0) if m else (s or "")


def _fullbox_version(f, body):
    f.seek(body)
    return f.read(1)[0]


def _mdhd(f, mdia):
    mdhd = _find(f, mdia[0], mdia[1], b"mdhd")
    if not mdhd:
        return 0, 0, ""
    ver = _fullbox_version(f, mdhd[0])
    if ver == 1:
        f.seek(mdhd[0] + 20)
        ts = _u32(f.read(4))
        dur = _u64(f.read(8))
        lang = _u16(f.read(2))
    else:
        f.seek(mdhd[0] + 12)
        ts = _u32(f.read(4))
        dur = _u32(f.read(4))
        lang = _u16(f.read(2))
    return ts, dur, _lang(lang)


def _stbl(f, mdia):
    minf = _find(f, mdia[0], mdia[1], b"minf")
    if not minf:
        return None
    return _find(f, minf[0], minf[1], b"stbl")


def _hdr_label(dv, transfer, static_meta):
    """Human label for a video's HDR signalling, or "" for SDR. dv = DV profile
    (-1 unknown), transfer = CICP (16 PQ, 18 HLG), static_meta = ST 2086/MaxCLL."""
    if dv is not None:
        return "Dolby Vision P%d" % dv if dv >= 0 else "Dolby Vision"
    if transfer == 16 or static_meta:
        return "HDR10"
    if transfer == 18:
        return "HLG"
    return ""


def _mp4_video_hdr(f, start, end, info):
    """Read DV/HDR boxes inside a VisualSampleEntry; sets info _dv/_transfer/_hdr10."""
    dv = None
    transfer = 0
    static_meta = False
    for typ, body, box_end in _children(f, start, end):
        if typ in (b"dvcC", b"dvvC", b"dvcE"):
            f.seek(body)
            data = f.read(min(5, box_end - body))
            dv = (data[2] >> 1) if len(data) >= 3 else -1
        elif typ == b"colr":
            f.seek(body)
            data = f.read(min(10, box_end - body))
            if len(data) >= 10 and data[:4] == b"nclx":
                transfer = _u16(data, 6)
        elif typ in (b"clli", b"mdcv"):
            static_meta = True
    if dv is None and info.get("fourcc") in (b"dvhe", b"dvh1", b"dva1", b"dvav"):
        dv = -1
    info["_dv"] = dv
    info["_transfer"] = transfer
    info["_hdr10"] = static_meta


def _track(f, trak):
    mdia = _find(f, trak[0], trak[1], b"mdia")
    if not mdia:
        return None
    hdlr = _find(f, mdia[0], mdia[1], b"hdlr")
    htype = b""
    if hdlr:
        f.seek(hdlr[0] + 8)
        htype = f.read(4)
    timescale, _dur, lang = _mdhd(f, mdia)
    stbl = _stbl(f, mdia)
    info = {"handler": htype, "timescale": timescale, "stbl": stbl, "lang": lang}
    if stbl:
        stsd = _find(f, stbl[0], stbl[1], b"stsd")
        if stsd:
            f.seek(stsd[0] + 8)  # skip version/flags + entry_count
            r = _box_header(f, stsd[1])
            if r:
                fourcc, body, _be = r
                info["fourcc"] = fourcc
                if htype == b"vide":
                    f.seek(body + 24)
                    info["width"] = _u16(f.read(2))
                    info["height"] = _u16(f.read(2))
                    _mp4_video_hdr(f, body + 78, _be, info)
                elif htype == b"soun":
                    f.seek(body + 16)
                    info["channels"] = _u16(f.read(2))
                    f.seek(body + 24)
                    info["sample_rate"] = _u32(f.read(4)) >> 16
        if htype == b"vide":
            stts = _find(f, stbl[0], stbl[1], b"stts")
            if stts:
                f.seek(stts[0] + 4)
                # Clamp the entry count to what fits the box: a crafted count
                # (up to 2^32) must not trigger a multi-GB read.
                box_avail = max(0, stts[1] - (stts[0] + 8)) // 8
                n = min(_u32(f.read(4)), box_avail)
                data = f.read(n * 8)
                total_s = total_d = 0
                for i in range(n):
                    sc = _u32(data, i * 8)
                    sd = _u32(data, i * 8 + 4)
                    total_s += sc
                    total_d += sc * sd
                if total_d and timescale:
                    info["fps"] = total_s / (total_d / float(timescale))
        stsz = _find(f, stbl[0], stbl[1], b"stsz")
        if stsz:
            f.seek(stsz[0] + 4)
            sample_size = _u32(f.read(4))
            count = _u32(f.read(4))
            box_avail = max(0, stsz[1] - (stsz[0] + 12)) // 4
            count = min(count, box_avail)
            if sample_size:
                total_bytes = sample_size * count
            else:
                blob = f.read(count * 4)
                total_bytes = sum(_u32(blob, i * 4) for i in range(count))
            if _dur and timescale:
                secs = _dur / float(timescale)
                if secs:
                    info["bitrate"] = total_bytes * 8 / secs / 1000.0
    return info


def _movie_duration(f, moov):
    mvhd = _find(f, moov[0], moov[1], b"mvhd")
    if not mvhd:
        return 0.0
    ver = _fullbox_version(f, mvhd[0])
    if ver == 1:
        f.seek(mvhd[0] + 20)
        ts = _u32(f.read(4))
        dur = _u64(f.read(8))
    else:
        f.seek(mvhd[0] + 12)
        ts = _u32(f.read(4))
        dur = _u32(f.read(4))
    return (dur / float(ts)) if ts else 0.0


def _read_mp4(path):
    """Return the tags + technical stream info found in an MP4/M4A file."""
    out = {}
    try:
        size = _size(path)
    except OSError:
        return out
    try:
        with _open_bin(path) as f:
            moov = _find(f, 0, size, b"moov")
            if not moov:
                return out
            dur = _movie_duration(f, moov)
            if dur:
                out["duration"] = dur
            # tags: moov > udta > meta > ilst
            udta = _find(f, moov[0], moov[1], b"udta")
            meta = _find(f, udta[0], udta[1], b"meta") if udta else None
            if meta:
                # meta is a FullBox (4 bytes version/flags); be tolerant
                ilst = _find(f, meta[0] + 4, meta[1], b"ilst") or _find(f, meta[0], meta[1], b"ilst")
                if ilst:
                    for typ, body, box_end in _children(f, ilst[0], ilst[1]):
                        if typ == b"\xa9day":
                            out["year"] = _year(_text(f, body, box_end))
                        elif typ in _TEXT_TAGS:
                            out[_TEXT_TAGS[typ]] = _text(f, body, box_end)
            for typ, body, box_end in _children(f, moov[0], moov[1]):
                if typ != b"trak":
                    continue
                t = _track(f, (body, box_end))
                if not t:
                    continue
                codec = _CODECS.get(t.get("fourcc"), "")
                if t["handler"] == b"vide":
                    if t.get("width"):
                        out["width"] = t["width"]
                        out["height"] = t.get("height", 0)
                    if codec:
                        out["vcodec"] = codec
                    if t.get("fps"):
                        out["fps"] = t["fps"]
                    if t.get("bitrate"):
                        out["bitrate"] = t["bitrate"]
                    hdr = _hdr_label(t.get("_dv"), t.get("_transfer", 0),
                                     t.get("_hdr10", False))
                    if hdr:
                        out["hdr"] = hdr
                elif t["handler"] == b"soun":
                    tr = {}
                    if codec:
                        tr["codec"] = codec
                    if t.get("channels"):
                        tr["channels"] = t["channels"]
                    if t.get("sample_rate"):
                        tr["sample_rate"] = t["sample_rate"]
                    if t.get("lang"):
                        tr["lang"] = t["lang"]
                    if tr:
                        out.setdefault("audio", []).append(tr)
                    if t.get("bitrate") and "bitrate" not in out:
                        out["bitrate"] = t["bitrate"]
            if out.get("audio"):  # first audio track also feeds the flat keys
                a0 = out["audio"][0]
                if a0.get("codec"):
                    out.setdefault("acodec", a0["codec"])
                if a0.get("channels"):
                    out.setdefault("channels", a0["channels"])
                if a0.get("sample_rate"):
                    out.setdefault("sample_rate", a0["sample_rate"])
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None)}


# ---- Matroska / WebM (EBML) ------------------------------------------------
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"
# Element ids (with marker bits, big-endian)
_ID_SEGMENT = 0x18538067
_ID_INFO = 0x1549A966
_ID_TIMECODE_SCALE = 0x2AD7B1
_ID_DURATION = 0x4489
_ID_TITLE = 0x7BA9
_ID_TRACKS = 0x1654AE6B
_ID_TRACK_ENTRY = 0xAE
_ID_TRACK_TYPE = 0x83
_ID_CODEC_ID = 0x86
_ID_DEFAULT_DURATION = 0x23E383
_ID_VIDEO = 0xE0
_ID_PIXEL_WIDTH = 0xB0
_ID_PIXEL_HEIGHT = 0xBA
_ID_AUDIO = 0xE1
_ID_LANGUAGE = 0x22B59C
_ID_NAME = 0x536E
_ID_SAMPLING_FREQ = 0xB5
_ID_CHANNELS = 0x9F
_ID_TAGS = 0x1254C367
_ID_TAG = 0x7373
_ID_SIMPLE_TAG = 0x67C8
_ID_TAG_NAME = 0x45A3
_ID_TAG_STRING = 0x4487
_ID_COLOUR = 0x55B0
_ID_TRANSFER = 0x55BA
_ID_MAXCLL = 0x55BC
_ID_MASTERING = 0x55D0
_ID_BLOCK_ADD = 0x41E4
_ID_BLOCK_ADD_TYPE = 0x41E7
_ID_BLOCK_ADD_EXTRA = 0x41ED
_ID_SEEKHEAD = 0x114D9B74
_ID_SEEK = 0x4DBB
_ID_SEEK_ID = 0x53AB
_ID_SEEK_POS = 0x53AC
_ID_CLUSTER = 0x1F43B675
_ID_CUES = 0x1C53BB6B
_ID_ATTACHMENTS = 0x1941A469
_ID_CHAPTERS = 0x1043A770
_ID_ATTACHED_FILE = 0x61A7
_ID_FILE_NAME = 0x466E
_ID_FILE_MIME = 0x4660
_ID_FILE_DATA = 0x465C
_DV_BLOCK_TYPES = (0x64766343, 0x64767643)  # 'dvcC' / 'dvvC'

# The MKV reader only scans the Segment head (Info/Tracks/SeekHead live in
# the first megabytes); past this budget SeekHead jumps are used.
_MKV_HEAD_BUDGET = 64 << 20

_MKV_CODECS = {
    "V_MPEG4/ISO/AVC": "h264", "V_MPEGH/ISO/HEVC": "hevc", "V_AV1": "av1",
    "V_VP9": "vp9", "V_VP8": "vp8", "V_MPEG4/ISO/ASP": "mpeg4",
    "V_MPEG2": "mpeg2", "V_THEORA": "theora", "V_MS/VFW/FOURCC": "vfw",
    "A_AAC": "aac", "A_AC3": "ac3", "A_EAC3": "eac3", "A_OPUS": "opus",
    "A_VORBIS": "vorbis", "A_FLAC": "flac", "A_MPEG/L3": "mp3",
    "A_DTS": "dts", "A_TRUEHD": "truehd", "A_MLP": "mlp",
    "A_PCM/INT/LIT": "pcm", "A_PCM/FLOAT/IEEE": "pcm",
}


def _ebml_id(f):
    b = f.read(1)
    if not b:
        return None
    first = b[0]
    length, mask = 1, 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 4:
            return None
    val = first
    for _ in range(length - 1):
        nb = f.read(1)
        if not nb:
            return None
        val = (val << 8) | nb[0]
    return val


def _ebml_size(f):
    b = f.read(1)
    if not b:
        return None
    first = b[0]
    length, mask = 1, 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 8:
            return None
    val = first & (mask - 1)
    for _ in range(length - 1):
        nb = f.read(1)
        if not nb:
            return None
        val = (val << 8) | nb[0]
    return val


def _ebml_elements(f, start, end):
    f.seek(start)
    while f.tell() < end:
        idv = _ebml_id(f)
        if idv is None:
            return
        size = _ebml_size(f)
        if size is None:
            return
        s = f.tell()
        e = s + size
        if e > end:
            e = end
        yield idv, s, e
        f.seek(e)


def _mkv_uint(f, s, e):
    f.seek(s)
    data = f.read(e - s)
    return int.from_bytes(data, "big") if data else 0


def _mkv_float(f, s, e):
    f.seek(s)
    data = f.read(e - s)
    if len(data) == 4:
        return struct.unpack(">f", data)[0]
    if len(data) == 8:
        return struct.unpack(">d", data)[0]
    return 0.0


def _mkv_str(f, s, e):
    f.seek(s)
    return f.read(e - s).decode("utf-8", "replace").strip("\x00").strip()


def _mkv_colour(f, start, end):
    """(transfer_characteristics, static HDR10 metadata present) from a Colour."""
    transfer = 0
    static_meta = False
    for idv, s, e in _ebml_elements(f, start, end):
        if idv == _ID_TRANSFER:
            transfer = _mkv_uint(f, s, e)
        elif idv in (_ID_MASTERING, _ID_MAXCLL):
            static_meta = True
    return transfer, static_meta


def _mkv_dv(f, start, end, dv):
    """Dolby Vision profile from a BlockAdditionMapping ('dvcC'/'dvvC')."""
    batype, extra = 0, None
    for idv, s, e in _ebml_elements(f, start, end):
        if idv == _ID_BLOCK_ADD_TYPE:
            batype = _mkv_uint(f, s, e)
        elif idv == _ID_BLOCK_ADD_EXTRA:
            extra = (s, e)
    if batype not in _DV_BLOCK_TYPES:
        return dv
    prof = -1
    if extra:
        f.seek(extra[0])
        data = f.read(min(5, extra[1] - extra[0]))
        if len(data) >= 3:
            prof = data[2] >> 1
    return prof


def _mkv_track(f, start, end, out):
    ttype, codec, default_dur = 0, "", 0
    width = height = channels = 0
    sfreq = 0.0
    lang = tname = ""
    video = audio = None
    dv = None
    for idv, s, e in _ebml_elements(f, start, end):
        if idv == _ID_TRACK_TYPE:
            ttype = _mkv_uint(f, s, e)
        elif idv == _ID_CODEC_ID:
            codec = _mkv_str(f, s, e)
        elif idv == _ID_DEFAULT_DURATION:
            default_dur = _mkv_uint(f, s, e)
        elif idv == _ID_LANGUAGE:
            lang = _mkv_str(f, s, e)
        elif idv == _ID_NAME:
            tname = _mkv_str(f, s, e)
        elif idv == _ID_VIDEO:
            video = (s, e)
        elif idv == _ID_AUDIO:
            audio = (s, e)
        elif idv == _ID_BLOCK_ADD:
            dv = _mkv_dv(f, s, e, dv)
    transfer = 0
    static_meta = False
    if video:
        for idv, s, e in _ebml_elements(f, video[0], video[1]):
            if idv == _ID_PIXEL_WIDTH:
                width = _mkv_uint(f, s, e)
            elif idv == _ID_PIXEL_HEIGHT:
                height = _mkv_uint(f, s, e)
            elif idv == _ID_COLOUR:
                transfer, static_meta = _mkv_colour(f, s, e)
    if audio:
        for idv, s, e in _ebml_elements(f, audio[0], audio[1]):
            if idv == _ID_SAMPLING_FREQ:
                sfreq = _mkv_float(f, s, e)
            elif idv == _ID_CHANNELS:
                channels = _mkv_uint(f, s, e)
    name = _MKV_CODECS.get(codec) or ""
    if not name and codec:
        name = codec.split("/")[-1].lower()
    if ttype == 1:
        if width:
            out["width"] = width
            out["height"] = height
        if name:
            out["vcodec"] = name
        if default_dur:
            out["fps"] = 1e9 / default_dur
        hdr = _hdr_label(dv, transfer, static_meta)
        if hdr:
            out["hdr"] = hdr
    elif ttype == 2:
        tr = {}
        if name:
            tr["codec"] = name
        if channels:
            tr["channels"] = channels
        if sfreq:
            tr["sample_rate"] = int(sfreq)
        if lang:
            tr["lang"] = lang.lower()
        if tname:
            tr["name"] = tname
        if tr:
            out.setdefault("audio", []).append(tr)


def _mkv_tags(f, start, end, out):
    for idv, s, e in _ebml_elements(f, start, end):
        if idv != _ID_TAG:
            continue
        for i2, s2, e2 in _ebml_elements(f, s, e):
            if i2 != _ID_SIMPLE_TAG:
                continue
            tname = tstr = ""
            for i3, s3, e3 in _ebml_elements(f, s2, e2):
                if i3 == _ID_TAG_NAME:
                    tname = _mkv_str(f, s3, e3).upper()
                elif i3 == _ID_TAG_STRING:
                    tstr = _mkv_str(f, s3, e3)
            if not (tname and tstr):
                continue
            if tname == "TITLE":
                out.setdefault("title", tstr)
            elif tname == "ARTIST":
                out.setdefault("artist", tstr)
            elif tname == "ALBUM":
                out.setdefault("album", tstr)
            elif tname in ("DATE_RELEASED", "DATE", "YEAR"):
                out.setdefault("year", _year(tstr))
            elif tname == "GENRE":
                out.setdefault("genre", tstr)
            elif tname == "ENCODER":
                out.setdefault("encoder", tstr)


def _mkv_seek_targets(f, start, end):
    """SeekID -> SeekPosition map from a SeekHead element."""
    targets = {}
    for idv, s, e in _ebml_elements(f, start, end):
        if idv != _ID_SEEK:
            continue
        sid = spos = None
        for i2, s2, e2 in _ebml_elements(f, s, e):
            if i2 == _ID_SEEK_ID:
                sid = _mkv_uint(f, s2, e2)
            elif i2 == _ID_SEEK_POS:
                spos = _mkv_uint(f, s2, e2)
        if sid is not None and spos is not None:
            targets[sid] = spos
    return targets


def _mkv_jump(f, seg_data, seg_elem, pos, want):
    """(start, end) of the element at SeekPosition `pos`, or None. The position
    may be relative to the data or the element start, so both bases are tried."""
    for base in (seg_data, seg_elem):
        try:
            f.seek(base + pos)
            rid = _ebml_id(f)
            if rid != want:
                continue
            size = _ebml_size(f)
            if size is None:
                continue
            s = f.tell()
            return s, s + size
        except Exception:
            continue
    return None


def _read_mkv(path):
    """Matroska/WebM headers without walking the media clusters: only the
    Segment head is scanned; the rest is reached via SeekHead jumps."""
    out = {}
    try:
        size = _size(path)
    except OSError:
        return out
    try:
        with _open_bin(path) as f:
            tscale, dur = 1000000, 0.0
            seg = None
            for idv, s, e in _ebml_elements(f, 0, size):
                if idv == _ID_SEGMENT:
                    seg = (s, e)
                    break
            if seg is None:
                return out
            sseg, eseg = seg
            # Segment element start (SeekPosition base): the 4-byte Segment id
            # sits right before the data start.
            seg_elem = sseg
            f.seek(max(0, sseg - 12))
            probe = f.read(sseg - max(0, sseg - 12))
            at = probe.rfind(b"\x18\x53\x80\x67")
            if at >= 0:
                seg_elem = max(0, sseg - 12) + at

            def _info_at(s2, e2):
                nonlocal tscale, dur
                for i3, s3, e3 in _ebml_elements(f, s2, e2):
                    if i3 == _ID_TIMECODE_SCALE:
                        tscale = _mkv_uint(f, s3, e3)
                    elif i3 == _ID_DURATION:
                        dur = _mkv_float(f, s3, e3)
                    elif i3 == _ID_TITLE:
                        t = _mkv_str(f, s3, e3)
                        if t:
                            out.setdefault("title", t)

            def _tracks_at(s2, e2):
                for i3, s3, e3 in _ebml_elements(f, s2, e2):
                    if i3 == _ID_TRACK_ENTRY:
                        _mkv_track(f, s3, e3, out)

            seekmap = {}
            have_info = have_tracks = have_tags = False
            limit = min(eseg, sseg + _MKV_HEAD_BUDGET)
            for i2, s2, e2 in _ebml_elements(f, sseg, eseg):
                if s2 >= limit:
                    break
                if i2 == _ID_INFO:
                    _info_at(s2, e2)
                    have_info = True
                elif i2 == _ID_TRACKS:
                    _tracks_at(s2, e2)
                    have_tracks = True
                elif i2 == _ID_SEEKHEAD:
                    for k, v in _mkv_seek_targets(f, s2, e2).items():
                        seekmap.setdefault(k, v)
                elif i2 == _ID_TAGS:
                    _mkv_tags(f, s2, e2, out)
                    have_tags = True
                if have_info and have_tracks and (
                        have_tags or _ID_TAGS in seekmap):
                    break
            # Whatever the head budget missed rides a SeekHead jump.
            for want, seen, parse in (
                    (_ID_INFO, have_info, _info_at),
                    (_ID_TRACKS, have_tracks, _tracks_at),
                    (_ID_TAGS, have_tags,
                     lambda s2, e2: _mkv_tags(f, s2, e2, out))):
                if seen or want not in seekmap:
                    continue
                at = _mkv_jump(f, sseg, seg_elem, seekmap[want], want)
                if at:
                    parse(*at)
            if out.get("audio"):  # first audio track also feeds the flat keys
                a0 = out["audio"][0]
                if a0.get("codec"):
                    out.setdefault("acodec", a0["codec"])
                if a0.get("channels"):
                    out.setdefault("channels", a0["channels"])
                if a0.get("sample_rate"):
                    out.setdefault("sample_rate", a0["sample_rate"])
            if dur and tscale:
                out["duration"] = dur * tscale / 1e9
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None)}


# ---- Shared tag maps -------------------------------------------------------
_VORBIS_MAP = {
    "TITLE": "title", "ARTIST": "artist", "ALBUM": "album", "GENRE": "genre",
    "DATE": "year", "YEAR": "year", "COMPOSER": "composer",
    "DESCRIPTION": "description", "COMMENT": "description",
    "ENCODEDBY": "encoder", "ENCODER": "encoder",
}
_ID3_MAP = {
    b"TIT2": "title", b"TPE1": "artist", b"TALB": "album",
    b"TYER": "year", b"TDRC": "year", b"TCON": "genre",
    b"TCOM": "composer", b"TENC": "encoder", b"TSSE": "encoder",
}


def _vorbis_comments(data, out, pos=0):
    """Parse a Vorbis comment block (FLAC body or Ogg packet) and merge the
    displayed fields."""
    if len(data) < pos + 8:
        return
    vlen = int.from_bytes(data[pos:pos + 4], "little")
    pos += 4 + vlen
    if pos + 4 > len(data):
        return
    count = int.from_bytes(data[pos:pos + 4], "little")
    pos += 4
    for _ in range(count):
        if pos + 4 > len(data):
            break
        clen = int.from_bytes(data[pos:pos + 4], "little")
        pos += 4
        if pos + clen > len(data):
            break
        item = data[pos:pos + clen]
        pos += clen
        if b"=" not in item:
            continue
        key, val = item.split(b"=", 1)
        dst = _VORBIS_MAP.get(key.decode("utf-8", "replace").strip().upper())
        val = val.decode("utf-8", "replace").strip()
        if dst and val and dst not in out:
            out[dst] = _year(val) if dst == "year" else val


# ---- MP3 (ID3v2 / ID3v1 + MPEG audio frame) --------------------------------
_MPEG_SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000),
            0: (11025, 12000, 8000)}
_MPEG_BR = {
    (3, 1): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (3, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (3, 3): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (2, 1): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
}
_MPEG_CODEC = {1: "mp3", 2: "mp2", 3: "mp1"}


def _synchsafe(b):
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _id3_decode(data, enc):
    try:
        if enc == 0:
            return data.decode("latin-1", "replace")
        if enc == 1:
            try:
                return data.decode("utf-16", "replace")
            except Exception:
                return data.decode("utf-16-be", "replace")
        if enc == 2:
            return data.decode("utf-16-be", "replace")
        return data.decode("utf-8", "replace")
    except Exception:
        return ""


def _id3_text(data):
    if not data:
        return ""
    return _id3_decode(data[1:], data[0]).replace("\x00", " ").strip()


def _id3_comment(data):
    if len(data) < 4:
        return ""
    txt = _id3_decode(data[4:], data[0])  # skip encoding + 3-byte language
    parts = [p for p in txt.split("\x00") if p.strip()]
    return parts[-1].strip() if parts else ""


def _id3v1(v, out):
    def s(b):
        return b.decode("latin-1", "replace").split("\x00")[0].strip()
    for key, val in (("title", v[0:30]), ("artist", v[30:60]),
                     ("album", v[60:90]), ("year", v[90:94]),
                     ("description", v[94:124])):
        txt = s(val)
        if txt and key not in out:
            out[key] = _year(txt) if key == "year" else txt


def _mp3_audio_info(f, start, size, out):
    """First MPEG audio frame -> codec, sample rate, channels, bitrate, duration
    (Xing/Info/VBRI frame count, else CBR estimate)."""
    off = start
    hdr = b""
    found = False
    for _ in range(4096):
        f.seek(off)
        hdr = f.read(4)
        if len(hdr) == 4 and hdr[0] == 0xFF and (hdr[1] & 0xE0) == 0xE0:
            found = True
            break
        off += 1
    if not found or len(hdr) < 4:
        # The fall-through leaves the last 4 bytes in `hdr` (bogus header).
        return
    mver, layer = (hdr[1] >> 3) & 3, (hdr[1] >> 1) & 3
    if mver == 1 or layer == 0:
        return
    sr_idx = (hdr[2] >> 2) & 3
    br_idx = (hdr[2] >> 4) & 0xF
    if sr_idx == 3:
        return
    grp = 3 if mver == 3 else 2
    out["sample_rate"] = _MPEG_SR[mver][sr_idx]
    out["channels"] = 1 if ((hdr[3] >> 6) & 3) == 3 else 2
    out["acodec"] = _MPEG_CODEC[layer]
    if br_idx in (0, 15):
        return
    bitrate = _MPEG_BR[(grp, layer)][br_idx]
    frames = 0
    if layer == 1:  # Layer III carries a Xing/Info/VBRI (VBR) header
        side = (17 if out["channels"] == 1 else 32) if grp == 3 else \
               (9 if out["channels"] == 1 else 17)
        f.seek(off + 4 + side)
        tag = f.read(4)
        if tag in (b"Xing", b"Info"):
            if _u32(f.read(4)) & 0x01:
                frames = _u32(f.read(4))
        else:
            f.seek(off + 36)
            if f.read(4) == b"VBRI":
                f.seek(off + 50)
                frames = _u32(f.read(4))
    if layer == 3:
        spf = 384
    elif layer == 2:
        spf = 1152
    else:
        spf = 1152 if grp == 3 else 576
    if frames:
        out["duration"] = frames * spf / float(out["sample_rate"])
        if out["duration"]:
            out["bitrate"] = (size - off) * 8 / out["duration"] / 1000.0
    else:
        out["bitrate"] = bitrate
        if bitrate:
            out["duration"] = (size - off) * 8 / (bitrate * 1000.0)


def _read_mp3(path):
    out = {}
    try:
        size = _size(path)
    except OSError:
        return out
    try:
        with _open_bin(path) as f:
            head = f.read(10)
            audio_start, frames = 0, {}
            if head[:3] == b"ID3":
                ver, flags = head[3], head[5]
                audio_start = end = 10 + _synchsafe(head[6:10])
                f.seek(10)
                if flags & 0x40:  # extended header
                    ext = f.read(4)
                    if len(ext) == 4:
                        esz = _synchsafe(ext) if ver >= 4 else _u32(ext)
                        f.seek(f.tell() + (esz - 4 if ver >= 4 else esz))
                while f.tell() + 10 <= end:
                    fh = f.read(10)
                    if len(fh) < 10 or fh[0] == 0:
                        break
                    sz = _synchsafe(fh[4:8]) if ver >= 4 else _u32(fh, 4)
                    if sz <= 0 or f.tell() + sz > end:
                        break
                    frames[fh[:4]] = f.read(sz)
            for fid, data in frames.items():
                dst = _ID3_MAP.get(fid)
                val = _id3_text(data) if dst else ""
                if dst and val and dst not in out:
                    out[dst] = _year(val) if dst == "year" else val
                elif fid == b"COMM" and "description" not in out:
                    com = _id3_comment(data)
                    if com:
                        out["description"] = com
            if not frames:
                f.seek(max(0, size - 128))
                if f.read(3) == b"TAG":
                    _id3v1(f.read(125), out)
            _mp3_audio_info(f, audio_start, size, out)
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


# ---- FLAC ------------------------------------------------------------------
def _read_flac(path):
    out = {}
    try:
        size = _size(path)
        with _open_bin(path) as f:
            if f.read(4) != b"fLaC":
                return out
            while True:
                bh = f.read(4)
                if len(bh) < 4:
                    break
                block_type, size = bh[0] & 0x7F, int.from_bytes(bh[1:4], "big")
                data = f.read(size)
                if block_type == 0 and len(data) >= 18:  # STREAMINFO
                    bits = int.from_bytes(data[10:18], "big")
                    out["sample_rate"] = (bits >> 44) & 0xFFFFF
                    out["channels"] = ((bits >> 41) & 0x7) + 1
                    total = bits & ((1 << 36) - 1)
                    if out["sample_rate"] and total:
                        out["duration"] = total / float(out["sample_rate"])
                elif block_type == 4:  # VORBIS_COMMENT
                    _vorbis_comments(data, out)
                if bh[0] & 0x80:
                    break
            out["acodec"] = "flac"
            if out.get("duration") and size:
                out["bitrate"] = size * 8 / out["duration"] / 1000.0
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


# ---- WAV (RIFF) ------------------------------------------------------------
_WAV_INFO = {b"INAM": "title", b"IART": "artist", b"IPRD": "album",
             b"ICRD": "year", b"IGNR": "genre", b"ICMT": "description",
             b"ISFT": "encoder"}


def _read_wav(path):
    out = {}
    try:
        with _open_bin(path) as f:
            if f.read(4) != b"RIFF":
                return out
            f.read(4)
            if f.read(4) != b"WAVE":
                return out
            byterate = datasize = 0
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    break
                cid, csz = ch[:4], _le32(ch, 4)
                pos = f.tell()
                # Cap the chunk read: a crafted RIFF size must not force a multi-GB allocation.
                data = f.read(min(csz, 1 << 20))
                if cid == b"fmt " and len(data) >= 16:
                    fmt = _le16(data, 0)
                    out["channels"] = _le16(data, 2)
                    out["sample_rate"] = _le32(data, 4)
                    byterate = _le32(data, 8)
                    out["acodec"] = "pcm" if fmt in (1, 3, 0xFFFE) else "adpcm"
                elif cid == b"data":
                    datasize = csz
                elif cid == b"LIST" and data[:4] == b"INFO":
                    p = 4
                    while p + 8 <= len(data):
                        iid, isz = data[p:p + 4], _le32(data, p + 4)
                        val = data[p + 8:p + 8 + isz]
                        dst = _WAV_INFO.get(iid)
                        txt = val.split(b"\x00")[0].decode("utf-8", "replace").strip()
                        if dst and txt and dst not in out:
                            out[dst] = _year(txt) if dst == "year" else txt
                        p += 8 + isz + (isz & 1)
                f.seek(pos + csz + (csz & 1))
            if byterate and datasize:
                out["duration"] = datasize / float(byterate)
                out["bitrate"] = byterate * 8 / 1000.0
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


def _extended80(b):
    """80-bit IEEE 754 extended float (AIFF sample rate)."""
    if len(b) < 10:
        return 0.0
    se = int.from_bytes(b[0:2], "big")
    exp, mant = se & 0x7FFF, int.from_bytes(b[2:10], "big")
    if exp == 0 or exp == 0x7FFF:
        return 0.0
    val = mant * 2.0 ** (exp - 16383 - 63)
    return -val if (se & 0x8000) else val


def _ogg_packets(f, limit=131072, max_packets=8):
    """Collect Ogg packets (packet bytes, stream serial) from the first pages."""
    buf = f.read(limit)
    packets, pos, cur, serial = [], 0, b"", 0
    while pos + 27 <= len(buf) and buf[pos:pos + 4] == b"OggS" \
            and len(packets) < max_packets:
        nseg = buf[pos + 26]
        seg = buf[pos + 27:pos + 27 + nseg]
        p = pos + 27 + nseg
        end = p + sum(seg)
        if end > len(buf):
            break
        serial = int.from_bytes(buf[pos + 14:pos + 18], "little")
        for s in seg:
            cur += buf[p:p + s]
            p += s
            if s < 255:
                packets.append((cur, serial))
                cur = b""
        pos = end
    return packets


def _ogg_last_granule(f, serial=None):
    """Granule of the last page (of `serial` when given) in the file tail."""
    try:
        f.seek(0, 2)
        size = f.tell()
        tail = min(size, 262144)
        f.seek(size - tail)
        buf = f.read(tail)
        i = len(buf)
        while True:
            i = buf.rfind(b"OggS", 0, i)
            if i < 0 or i + 18 > len(buf):
                return 0
            if serial is None or int.from_bytes(buf[i + 14:i + 18], "little") == serial:
                return int.from_bytes(buf[i + 6:i + 14], "little")
    except Exception:
        return 0


def _read_ogg(path):
    """Ogg: identify the audio (Vorbis/Opus) and video (Theora) streams
    independently; duration from the audio stream's last page."""
    out = {}
    try:
        size = _size(path)
        with _open_bin(path) as f:
            packets = _ogg_packets(f)
            audio_kind = video_kind = ""
            a_serial = v_serial = 0
            a_rate = preskip = 0
            for pk, serial in packets:
                if pk[:7] == b"\x01vorbis" and not audio_kind:
                    audio_kind, a_serial = "vorbis", serial
                    out["acodec"] = "vorbis"
                    out["channels"] = pk[11]
                    a_rate = int.from_bytes(pk[12:16], "little")
                    out["sample_rate"] = a_rate
                elif pk[:8] == b"OpusHead" and not audio_kind:
                    audio_kind, a_serial = "opus", serial
                    out["acodec"] = "opus"
                    out["channels"] = pk[9]
                    preskip = int.from_bytes(pk[10:12], "little")
                    a_rate = int.from_bytes(pk[12:16], "little") or 48000
                    out["sample_rate"] = a_rate
                elif pk[:7] == b"\x80theora" and not video_kind:
                    video_kind, v_serial = "theora", serial
                    out["vcodec"] = "theora"
                    if len(pk) >= 19:
                        out["width"] = int.from_bytes(pk[13:16], "big")
                        out["height"] = int.from_bytes(pk[16:19], "big")
                    if len(pk) >= 29:
                        frn = int.from_bytes(pk[22:26], "big")
                        frd = int.from_bytes(pk[26:30], "big")
                        if frd:
                            out["fps"] = frn / frd
            if not audio_kind and not video_kind:
                return out
            for pk, _serial in packets:
                if pk[:7] == b"\x03vorbis" and audio_kind == "vorbis":
                    _vorbis_comments(pk, out, 7)
                elif pk[:8] == b"OpusTags" and audio_kind == "opus":
                    _vorbis_comments(pk, out, 8)
                elif pk[:7] == b"\x81theora" and video_kind == "theora":
                    _vorbis_comments(pk, out, 7)
            if audio_kind:  # duration from the AUDIO stream's last page
                granule = _ogg_last_granule(f, a_serial)
                if granule:
                    if audio_kind == "vorbis" and a_rate:
                        out["duration"] = granule / float(a_rate)
                    elif audio_kind == "opus":
                        out["duration"] = max(0.0, granule - preskip) / 48000.0
            if out.get("duration") and size:
                out["bitrate"] = size * 8 / out["duration"] / 1000.0
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


_FLV_VIDEO = {2: "h263", 3: "screen", 4: "vp6", 5: "vp6a", 6: "screen2",
              7: "h264", 12: "hevc", 13: "av1"}
_FLV_AUDIO = {0: "pcm", 1: "adpcm", 2: "mp3", 4: "nellymoser", 5: "nellymoser",
              6: "nellymoser", 7: "pcma", 8: "pcmu", 10: "aac", 11: "speex",
              14: "mp3"}


def _flv_amf(b, i):
    """Minimal AMF0 reader -> (value, next_index)."""
    if i >= len(b):
        return None, i
    t = b[i]
    i += 1
    if t == 0x00:
        return struct.unpack(">d", b[i:i + 8])[0], i + 8
    if t == 0x01:
        return bool(b[i]), i + 1
    if t == 0x02:
        n = _u16(b, i)
        return b[i + 2:i + 2 + n].decode("utf-8", "replace"), i + 2 + n
    if t in (0x03, 0x08):
        if t == 0x08:
            i += 4  # ECMA array count (only approximate)
        obj = {}
        while i + 2 <= len(b):
            nl = _u16(b, i)
            i += 2
            if nl == 0:
                i += 1  # object end marker
                break
            key = b[i:i + nl].decode("utf-8", "replace")
            i += nl
            val, i = _flv_amf(b, i)
            obj[key] = val
        return obj, i
    if t == 0x0A:
        n = _u32(b, i)
        i += 4
        arr = []
        for _ in range(n):
            val, i = _flv_amf(b, i)
            arr.append(val)
        return arr, i
    return None, i


def _read_flv(path):
    """FLV: onMetaData script tag (duration/size/fps/rates) + first video/audio
    tag codec ids when metadata omits them."""
    out = {}
    try:
        size = _size(path)
        with _open_bin(path) as f:
            if f.read(3) != b"FLV":
                return out
            f.read(2)
            offset = _u32(f.read(4))
            f.seek(offset + 4)  # skip PreviousTagSize0 before the first tag
            meta, vcodec, acodec, tags = {}, None, None, 0
            while f.tell() + 11 <= size and tags < 200:
                h = f.read(11)
                if len(h) < 11:
                    break
                dsize = (h[1] << 16) | (h[2] << 8) | h[3]
                data = f.read(dsize)
                f.read(4)
                tags += 1
                if h[0] == 18 and not meta:
                    name, i = _flv_amf(data, 0)
                    if isinstance(name, str) and name.lower() == "onmetadata":
                        val, _i = _flv_amf(data, i)
                        if isinstance(val, dict):
                            meta = val
                elif h[0] == 9 and vcodec is None and data:
                    vcodec = data[0] >> 4
                elif h[0] == 8 and acodec is None and data:
                    acodec = data[0] >> 4
                if meta and vcodec is not None and acodec is not None:
                    break
            if meta.get("duration"):
                out["duration"] = float(meta["duration"])
            for key, dst in (("width", "width"), ("height", "height")):
                if meta.get(key):
                    out[dst] = int(meta[key])
            if meta.get("framerate"):
                out["fps"] = float(meta["framerate"])
            if meta.get("audiosamplerate"):
                out["sample_rate"] = int(meta["audiosamplerate"])
            if meta.get("audiochannels"):
                out["channels"] = int(meta["audiochannels"])
            if meta.get("videocodecid"):
                vcodec = int(meta["videocodecid"])
            if meta.get("audiocodecid"):
                acodec = int(meta["audiocodecid"])
            if vcodec is not None:
                out["vcodec"] = _FLV_VIDEO.get(vcodec, "video")
            if acodec is not None:
                out["acodec"] = _FLV_AUDIO.get(acodec, "audio")
            if out.get("duration"):
                out["bitrate"] = size * 8 / out["duration"] / 1000.0
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


def _read_aiff(path):
    """AIFF / AIFC: COMM (channels, frames, bits, 80-bit rate) + NAME/AUTH/ANNO."""
    out = {}
    try:
        with _open_bin(path) as f:
            if f.read(4) != b"FORM":
                return out
            f.read(4)
            if f.read(4) not in (b"AIFF", b"AIFC"):
                return out
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    break
                cid, csz = ch[:4], _u32(ch, 4)
                pos = f.tell()
                data = f.read(min(csz, 1 << 20))
                if cid == b"COMM" and len(data) >= 18:
                    rate = _extended80(data[8:18])
                    out["channels"] = _u16(data, 0)
                    frames = _u32(data, 2)
                    out["acodec"] = "pcm"
                    if rate:
                        out["sample_rate"] = int(round(rate))
                        if frames:
                            out["duration"] = frames / rate
                    if out.get("sample_rate"):
                        out["bitrate"] = out["sample_rate"] * out.get("channels", 1) \
                                         * _u16(data, 6) / 1000.0
                elif cid in (b"NAME", b"AUTH", b"ANNO"):
                    txt = data.split(b"\x00")[0].decode("utf-8", "replace").strip()
                    dst = {b"NAME": "title", b"AUTH": "artist",
                           b"ANNO": "description"}[cid]
                    if txt and dst not in out:
                        out[dst] = txt
                f.seek(pos + csz + (csz & 1))
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


_AAC_SR = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000,
           12000, 11025, 8000, 7350)
_AAC_CH = {0: 2, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 8}
AAC_SAMPLE_BYTES = 2 * 1024 * 1024  # frame-sampling window (bounded read)
AAC_SAMPLE_FRAMES = 3000


def _adts_header(b):
    """(sample_rate, channels, frame_length, blocks) from a 7-byte ADTS header."""
    if len(b) < 7 or b[0] != 0xFF or (b[1] & 0xF0) != 0xF0:
        return None
    sr_idx = (b[2] >> 2) & 0xF
    chan = ((b[2] & 1) << 2) | ((b[3] >> 6) & 3)
    flen = ((b[3] & 3) << 11) | (b[4] << 3) | ((b[5] >> 5) & 7)
    return (_AAC_SR[sr_idx] if sr_idx < len(_AAC_SR) else 0,
            _AAC_CH.get(chan, 2), flen, (b[6] & 3) + 1)


def _read_aac(path):
    """Raw ADTS AAC: parse a bounded window of frames and average the frame
    length, then estimate duration/bitrate from the file size. CBR is accurate,
    VBR is close (a full frame walk read the whole file -- slow on a network
    share for one metadata read)."""
    out = {}
    try:
        size = _size(path)
        with _open_bin(path) as f:
            head = f.read(10)
            start = 0
            if head[:3] == b"ID3":
                start = 10 + _synchsafe(head[6:10])
            f.seek(start)
            buf = f.read(AAC_SAMPLE_BYTES)
            i = 0
            n = len(buf)
            sr = ch = 0
            total = blocks = frames = 0
            while frames < AAC_SAMPLE_FRAMES and n - i >= 7:
                h = _adts_header(buf[i:i + 7])
                if not h or not h[0] or h[2] < 7 or i + h[2] > n:
                    break
                if frames == 0:
                    sr, ch = h[0], h[1]
                total += h[2]
                blocks += h[3]
                frames += 1
                i += h[2]
            if not frames:
                return out
            out["acodec"] = "aac"
            out["sample_rate"], out["channels"] = sr, ch
            if sr and total and size > start:
                avg_flen = total / float(frames)
                frame_secs = (blocks / float(frames)) * 1024 / float(sr)
                out["duration"] = ((size - start) / avg_flen) * frame_secs
                out["bitrate"] = (avg_flen * 8.0 / frame_secs) / 1000.0
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


def _le64(b, o=0):
    return struct.unpack_from("<Q", b, o)[0]


_ASF_HEADER = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_ASF_FILE_PROPS = bytes.fromhex("04b2008f80e9f84c99598ad417f4be4c")
_ASF_STREAM_PROPS = bytes.fromhex("9107dcb7b7a9cf118ee600c00c205365")
_ASF_CONTENT_DESC = bytes.fromhex("3326b2758e66cf11a6d900aa0062ce6c")
_ASF_EXT_CONTENT_DESC = bytes.fromhex("40a4d0d207e3d21197f000a0c95ea850")
_ASF_AUDIO_MEDIA = bytes.fromhex("409e69f84d5bcf11a8fd00805f5c442b")
_ASF_VIDEO_MEDIA = bytes.fromhex("c0ef19bc4d5bcf11a8fd00805f5c442b")
_ASF_AUDIO = {0x0160: "wma", 0x0161: "wma", 0x0162: "wmapro",
              0x0163: "wmalossless", 0x000A: "wmavoice"}
_ASF_EXT_MAP = {"WM/ALBUMTITLE": "album", "WM/GENRE": "genre", "WM/YEAR": "year",
                "WM/COMPOSER": "composer", "WM/ENCODEDBY": "encoder"}


def _asf_objects(f, start, end):
    """Yield (guid, data_start, data_end) for the ASF objects in [start, end)."""
    while start + 24 <= end:
        f.seek(start)
        guid = f.read(16)
        sz = _le64(f.read(8))
        if sz < 24 or start + sz > end:
            break
        yield guid, start + 24, start + sz
        start += sz


def _asf_stream(data, out):
    if len(data) < 54:
        return
    stype = data[0:16]
    ts = data[54:54 + _le32(data, 40)]  # type-specific data
    if stype == _ASF_AUDIO_MEDIA and len(ts) >= 16:
        out["acodec"] = _ASF_AUDIO.get(_le16(ts, 0), "audio")
        out["channels"] = _le16(ts, 2)
        out["sample_rate"] = _le32(ts, 4)
        if _le32(ts, 8):
            out["_abytes"] = _le32(ts, 8)
    elif stype == _ASF_VIDEO_MEDIA and len(ts) >= 20:
        out["width"] = _le32(ts, 4)
        out["height"] = _le32(ts, 8)
        fourcc = ts[16:20].decode("latin-1", "replace").strip("\x00").strip().lower()
        if fourcc:
            out["vcodec"] = fourcc


def _asf_content(data, out):
    p = 0
    for dst in ("title", "artist", None, "description", None):
        if p + 2 > len(data):
            break
        n = _le16(data, p)
        p += 2
        txt = data[p:p + n].decode("utf-16-le", "replace").strip("\x00").strip()
        p += n
        if dst and txt and dst not in out:
            out[dst] = txt


def _asf_ext_content(data, out):
    if len(data) < 2:
        return
    count, p = _le16(data, 0), 2
    for _ in range(count):
        if p + 2 > len(data):
            break
        nlen = _le16(data, p)
        p += 2
        name = data[p:p + nlen].decode("utf-16-le", "replace").strip("\x00").strip().upper()
        p += nlen
        if p + 4 > len(data):
            break
        vtype, vlen = _le16(data, p), _le16(data, p + 2)
        raw = data[p + 4:p + 4 + vlen]
        p += 4 + vlen
        dst = _ASF_EXT_MAP.get(name)
        if not dst or not raw:
            continue
        if vtype == 0:
            val = raw.decode("utf-16-le", "replace").strip("\x00").strip()
        elif vtype == 3:
            val = str(int.from_bytes(raw[:4], "little"))
        elif vtype == 4:
            val = str(int.from_bytes(raw[:8], "little"))
        else:
            continue
        if val and dst not in out:
            out[dst] = _year(val) if dst == "year" else val


def _read_asf(path):
    """ASF (WMA/WMV): File Properties (duration), Stream Properties (codec,
    audio/video), Content + Extended Content Description (tags)."""
    out = {}
    try:
        with _open_bin(path) as f:
            guid = f.read(16)
            size = _le64(f.read(8))
            if guid != _ASF_HEADER:
                return out
            f.read(6)  # object count + 2 reserved
            for g, s, e in _asf_objects(f, f.tell(), size):
                f.seek(s)
                data = f.read(e - s)
                if g == _ASF_FILE_PROPS and len(data) >= 64:
                    play = int.from_bytes(data[40:48], "little")   # 100 ns
                    preroll = int.from_bytes(data[56:64], "little")  # ms
                    dur = play / 1e7 - preroll / 1000.0
                    if dur > 0:
                        out["duration"] = dur
                elif g == _ASF_STREAM_PROPS:
                    _asf_stream(data, out)
                elif g == _ASF_CONTENT_DESC:
                    _asf_content(data, out)
                elif g == _ASF_EXT_CONTENT_DESC:
                    _asf_ext_content(data, out)
            if out.get("_abytes") and not out.get("vcodec"):
                out["bitrate"] = out.pop("_abytes") * 8 / 1000.0
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


def _avi_chunks(f, start, end):
    while start + 8 <= end:
        f.seek(start)
        cid = f.read(4)
        if len(cid) < 4:
            return
        csz = _le32(f.read(4))
        yield cid, start + 8, start + 8 + csz
        start += 8 + csz + (csz & 1)


def _avi_strl(f, start, end, out):
    stype, strh, strf = None, None, None
    for cid, s, e in _avi_chunks(f, start, end):
        if cid == b"strh":
            f.seek(s)
            d = f.read(36)
            if len(d) >= 36:
                stype = d[0:4]
                scale, rate, length = _le32(d, 20), _le32(d, 24), _le32(d, 32)
                if stype == b"vids" and rate and scale:
                    if not out.get("fps"):
                        out["fps"] = rate / scale
                    if length and "duration" not in out:
                        out["duration"] = length / (rate / scale)
        elif cid == b"strf":
            strf = (s, e)
    if stype == b"vids" and strf:
        f.seek(strf[0])
        d = f.read(20)
        if len(d) >= 20:
            if _le32(d, 4):
                out["width"], out["height"] = _le32(d, 4), _le32(d, 8)
            fourcc = d[16:20].decode("latin-1", "replace").strip("\x00").strip().lower()
            if fourcc:
                out["vcodec"] = fourcc
    elif stype == b"auds" and strf:
        f.seek(strf[0])
        d = f.read(16)
        if len(d) >= 16:
            fmt = _le16(d, 0)
            out["acodec"] = {1: "pcm", 0x11: "adpcm", 0x50: "mp2",
                             0x55: "mp3", 0xFF: "aac", 0x2000: "ac3"}.get(fmt, "audio")
            out["channels"] = _le16(d, 2)
            out["sample_rate"] = _le32(d, 4)


def _read_avi(path):
    """RIFF AVI: avih (size/fps/frames) + strl (codec) + INFO (tags)."""
    out = {}
    try:
        size = _size(path)
        with _open_bin(path) as f:
            if f.read(4) != b"RIFF":
                return out
            f.read(4)
            if f.read(4) != b"AVI ":
                return out
            for cid, s, e in _avi_chunks(f, 12, size):
                if cid != b"LIST":
                    continue
                f.seek(s)
                typ = f.read(4)
                if typ == b"hdrl":
                    for c2, s2, e2 in _avi_chunks(f, s + 4, e):
                        if c2 == b"avih":
                            f.seek(s2)
                            d = f.read(40)
                            if len(d) >= 40:
                                out["width"], out["height"] = _le32(d, 32), _le32(d, 36)
                                if _le32(d, 0) and not out.get("fps"):
                                    out["fps"] = 1e6 / _le32(d, 0)
                        elif c2 == b"LIST":
                            f.seek(s2)
                            if f.read(4) == b"strl":
                                _avi_strl(f, s2 + 4, e2, out)
                elif typ == b"INFO":
                    for c2, s2, e2 in _avi_chunks(f, s + 4, e):
                        dst = {b"INAM": "title", b"IART": "artist", b"IPRD": "album",
                               b"ICRD": "year", b"IGNR": "genre", b"ICMT": "description",
                               b"ISFT": "encoder"}.get(c2)
                        if dst:
                            f.seek(s2)
                            txt = f.read(e2 - s2).split(b"\x00")[0].decode("utf-8", "replace").strip()
                            if txt and dst not in out:
                                out[dst] = _year(txt) if dst == "year" else txt
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


class _Bits:
    def __init__(self, data):
        self.d, self.pos = data, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            if self.pos >> 3 >= len(self.d):
                raise ValueError("eof")
            v = (v << 1) | ((self.d[self.pos >> 3] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 31:
                raise ValueError("ue")
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _rbsp(data):
    """Remove H.264/HEVC emulation-prevention bytes (00 00 03 -> 00 00)."""
    out, zeros = bytearray(), 0
    for b in data:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def _sps_h264(rbsp):
    """(width, height) from an H.264 SPS RBSP, or None."""
    try:
        b = _Bits(rbsp)
        profile = b.u(8)
        b.u(8)
        b.u(8)
        b.ue()  # sps id
        chroma = 1
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma = b.ue()
            if chroma == 3:
                b.u(1)
            b.ue()
            b.ue()
            b.u(1)
            if b.u(1):
                for i in range(8 if chroma != 3 else 12):
                    if b.u(1):
                        last, nxt = 8, 8
                        for _ in range(16 if i < 6 else 64):
                            if nxt:
                                nxt = (last + b.se() + 256) % 256
                            last = nxt or last
        b.ue()  # log2_max_frame_num
        poc = b.ue()
        if poc == 0:
            b.ue()
        elif poc == 1:
            b.u(1)
            b.se()
            b.se()
            for _ in range(b.ue()):
                b.se()
        b.ue()  # max_num_ref_frames
        b.u(1)  # gaps
        w = (b.ue() + 1) * 16
        h_map = b.ue() + 1
        frame_only = b.u(1)
        if not frame_only:
            b.u(1)
        b.u(1)
        cl = cr = ct = cb = 0
        if b.u(1):
            cl, cr, ct, cb = b.ue(), b.ue(), b.ue(), b.ue()
        sub_w = 2 if chroma in (1, 2) else 1
        sub_h = 2 if chroma == 1 else 1
        width = w - (cl + cr) * sub_w
        height = (2 - frame_only) * h_map * 16 - (ct + cb) * sub_h * (2 - frame_only)
        return width, height
    except Exception:
        return None


def _sps_hevc(rbsp):
    """(width, height) from an HEVC SPS RBSP, or None."""
    try:
        b = _Bits(rbsp)
        b.u(4)
        max_sub = b.u(3)
        b.u(1)
        b.u(2)
        b.u(1)
        b.u(5)
        b.u(32)
        b.u(48)
        b.u(8)
        flags = [(b.u(1), b.u(1)) for _ in range(max_sub)]
        if max_sub:
            for _ in range(max_sub, 8):
                b.u(2)
        for pf, lf in flags:
            if pf:
                b.u(2)
                b.u(1)
                b.u(5)
                b.u(32)
                b.u(48)
            if lf:
                b.u(8)
        b.ue()  # sps id
        chroma = b.ue()
        if chroma == 3:
            b.u(1)
        width = b.ue()
        height = b.ue()
        if b.u(1):
            cl, cr, ct, cb = b.ue(), b.ue(), b.ue(), b.ue()
            sw, sh = (2, 2) if chroma == 1 else ((2, 1) if chroma == 2 else (1, 1))
            width -= (cl + cr) * sw
            height -= (ct + cb) * sh
        return width, height
    except Exception:
        return None


def _find_video_size(blob):
    """Best-effort (vcodec, width, height) from an elementary-stream blob."""
    i = blob.find(b"\x00\x00\x01\x67")  # H.264 SPS
    if i >= 0:
        nxt = blob.find(b"\x00\x00\x01", i + 4)
        r = _sps_h264(_rbsp(blob[i + 4:nxt if nxt > 0 else i + 96]))
        if r and r[0] and r[1]:
            return "h264", r[0], r[1]
    i = blob.find(b"\x00\x00\x01\x42\x01")  # HEVC SPS
    if i >= 0:
        nxt = blob.find(b"\x00\x00\x01", i + 5)
        r = _sps_hevc(_rbsp(blob[i + 5:nxt if nxt > 0 else i + 128]))
        if r and r[0] and r[1]:
            return "hevc", r[0], r[1]
    i = blob.find(b"\x00\x00\x01\xb3")  # MPEG-2 sequence header
    if i >= 0 and i + 7 <= len(blob):
        d = blob[i + 4:i + 8]
        return "mpeg2", (d[0] << 4) | (d[1] >> 4), ((d[1] & 0xF) << 8) | d[2]
    return None


def _find_pts(blob, stream_ids, last=False):
    """First or last PES PTS (90 kHz) for the given stream ids, or None."""
    found = None
    i = 0
    while True:
        i = blob.find(b"\x00\x00\x01", i)
        if i < 0 or i + 14 > len(blob):
            break
        if blob[i + 3] in stream_ids and (blob[i + 7] & 0x80):
            if (blob[i + 7] >> 6) & 3:
                p = blob[i + 9:i + 14]
                found = (((p[0] >> 1) & 7) << 30) | (p[1] << 22) \
                        | (((p[2] >> 1) & 0x7F) << 15) | (p[3] << 7) | (p[4] >> 1)
                if not last:
                    return found
        i += 4
    return found


_TS_TYPES = {
    0x01: "v:mpeg2", 0x02: "v:mpeg2", 0x10: "v:mpeg4", 0x1B: "v:h264",
    0x24: "v:hevc", 0xEA: "v:vc1",
    0x03: "a:mp2", 0x04: "a:mp2", 0x0F: "a:aac", 0x11: "a:aac",
    0x81: "a:ac3", 0x87: "a:eac3", 0x82: "a:dts", 0x86: "a:dts",
    # 0x06 (PES private data) is ambiguous (subtitles/teletext AND AC-3/
    # E-AC-3/DTS); classified from the ES descriptors in _ts_kind().
}


def _ts_kind(stype, desc):
    """Stream kind from the stream_type and its ES descriptors."""
    kind = _TS_TYPES.get(stype)
    if kind:
        return kind
    if stype == 0x06:
        p = 0
        while p + 2 <= len(desc):
            tag, ln = desc[p], desc[p + 1]
            if tag == 0x6A:
                return "a:ac3"
            if tag == 0x7A:
                return "a:eac3"
            if tag == 0x7B:
                return "a:dts"
            p += 2 + ln
    return None


def _ts_lang(desc):
    """ISO-639 language from the PMT ES descriptors (tag 0x0A)."""
    p = 0
    while p + 2 <= len(desc):
        tag, ln = desc[p], desc[p + 1]
        if tag == 0x0A and ln >= 3:
            return desc[p + 2:p + 5].decode("latin-1", "replace").lower()
        p += 2 + ln
    return ""


def _ac3_header(b):
    """(sample_rate, channels) from an AC-3 sync frame, or None."""
    if len(b) < 7 or b[0] != 0x0B or b[1] != 0x77:
        return None
    sr = {0: 48000, 1: 44100, 2: 32000}.get(b[4] >> 6)
    if not sr:
        return None
    ch = {0: 2, 1: 1, 2: 2, 3: 3, 4: 3, 5: 4, 6: 4, 7: 5}.get(b[6] >> 5, 2)
    return sr, ch


def _ts_packet_size(head):
    for psize, sync in ((188, 0), (192, 4), (204, 0)):
        if len(head) >= 2 * psize and head[sync] == 0x47 and head[sync + psize] == 0x47:
            return psize, sync
    return 188, 0


def _ts_blob(f, start, end, pid, psize, sync):
    """Concatenated TS payloads of `pid` in [start, end)."""
    start = max(0, start)
    f.seek(start)
    data = f.read(max(0, end - start))
    blob = bytearray()
    for i in range(0, len(data) - psize + 1, psize):
        pkt = data[i + sync:i + sync + 188]
        if len(pkt) < 188 or pkt[0] != 0x47:
            continue
        if (((pkt[1] & 0x1F) << 8) | pkt[2]) != pid:
            continue
        p = 4
        if ((pkt[3] >> 4) & 3) in (2, 3):
            p = 5 + pkt[4]
        if p < 188:
            blob += pkt[p:188]
    return bytes(blob)


def _read_ts(path):
    """MPEG-TS: PAT/PMT stream types, SPS/sequence-header resolution, PTS duration."""
    out = {}
    try:
        size = _size(path)
        with _open_bin(path) as f:
            psize, sync = _ts_packet_size(f.read(4096))
            win = min(size, 4 << 20)
            streams = []
            pmt_pid = None
            for i in range(0, win - psize + 1, psize):
                f.seek(i)
                pkt = f.read(188)
                if len(pkt) < 188 or pkt[0] != 0x47:
                    continue
                pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
                if not (pkt[1] & 0x40) or pid not in (0, pmt_pid):
                    continue
                p = 4
                if ((pkt[3] >> 4) & 3) in (2, 3):
                    p = 5 + pkt[4]
                if p >= 188:
                    continue
                t = pkt[p + 1:]  # skip pointer_field
                if pid == 0 and len(t) >= 8 and t[0] == 0x00:
                    end = 3 + (((t[1] & 0x0F) << 8) | t[2])
                    j = 8
                    while j + 4 <= min(end, len(t)):
                        if ((t[j] << 8) | t[j + 1]) != 0:
                            pmt_pid = ((t[j + 2] & 0x1F) << 8) | t[j + 3]
                        j += 4
                elif pid == pmt_pid and len(t) >= 12 and t[0] == 0x02:
                    end = 3 + (((t[1] & 0x0F) << 8) | t[2])
                    j = 12 + (((t[10] & 0x0F) << 8) | t[11])
                    while j + 5 <= min(end, len(t)):
                        esil = ((t[j + 3] & 0x0F) << 8) | t[j + 4]
                        desc = t[j + 5:j + 5 + esil]
                        streams.append((t[j], ((t[j + 1] & 0x1F) << 8) | t[j + 2],
                                        _ts_lang(desc), desc))
                        j += 5 + esil
            vpid, audios = None, []
            for stype, spid, slang, desc in streams:
                kind = _ts_kind(stype, desc)
                if not kind:
                    continue
                if kind[0] == "v" and vpid is None:
                    vpid, out["vcodec"] = spid, kind[2:]
                elif kind[0] == "a":
                    tr = {"codec": kind[2:]}
                    if slang:
                        tr["lang"] = slang
                    audios.append((spid, tr))
            for spid, tr in audios:
                blob = _ts_blob(f, 0, win, spid, psize, sync)
                if tr["codec"] == "aac":
                    k = blob.find(b"\xff\xf1")
                    if k < 0:
                        k = blob.find(b"\xff\xf9")
                    det = _adts_header(blob[k:k + 7]) if k >= 0 else None
                else:
                    k = blob.find(b"\x0b\x77")
                    det = _ac3_header(blob[k:k + 8]) if k >= 0 else None
                if det:
                    tr["sample_rate"], tr["channels"] = det[0], det[1]
            if audios:
                out["audio"] = [tr for _p, tr in audios]
                out["acodec"] = audios[0][1]["codec"]
                for k in ("sample_rate", "channels"):
                    if audios[0][1].get(k):
                        out.setdefault(k, audios[0][1][k])
            if vpid is not None:
                info = _find_video_size(_ts_blob(f, 0, win, vpid, psize, sync))
                if info:
                    out["vcodec"] = info[0]
                    out["width"], out["height"] = info[1], info[2]
                first = _find_pts(_ts_blob(f, 0, min(size, 1 << 20), vpid, psize, sync), range(0xE0, 0xF0))
                tail = min(size, 4 << 20)
                last = _find_pts(_ts_blob(f, size - tail, size, vpid, psize, sync), range(0xE0, 0xF0), True)
                if first is not None and last is not None and last > first:
                    out["duration"] = (last - first) / 90000.0
            if out.get("duration") and out.get("vcodec") and "bitrate" not in out:
                out["bitrate"] = size * 8 / out["duration"] / 1000.0
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


def _read_ps(path):
    """MPEG program/elementary stream: stream ids, sequence-header or SPS
    resolution, PTS duration."""
    out = {}
    try:
        size = _size(path)
        with _open_bin(path) as f:
            win = min(size, 4 << 20)
            blob = f.read(win)
            vid = any(blob.find(b"\x00\x00\x01" + bytes([s])) >= 0 for s in (0xE0, 0xE1))
            info = _find_video_size(blob)
            if info:
                out["vcodec"], out["width"], out["height"] = info
            elif vid:
                out["vcodec"] = "mpeg2"
            if blob.find(b"\x00\x00\x01\xc0") >= 0 or blob.find(b"\x00\x00\x01\xc1") >= 0:
                out["acodec"] = "mp2"
            elif blob.find(b"\x00\x00\x01\xbd") >= 0:
                out["acodec"] = "ac3"
            first = _find_pts(blob, range(0xE0, 0xF0))
            tail = min(size, 4 << 20)
            f.seek(max(0, size - tail))
            last = _find_pts(f.read(tail), range(0xE0, 0xF0), True)
            if first is not None and last is not None and last > first:
                out["duration"] = (last - first) / 90000.0
    except Exception:
        pass
    return {k: v for k, v in out.items() if v not in ("", None, 0)}


def _size(path):
    """Size of the media file. Local paths use os.path.getsize (raises OSError);
    a network URL uses the VFS File.size() -- getsize cannot stat a davs:// URL."""

    def _call():
        try:
            import sources
            if sources.is_network_path(path):
                import xbmcvfs
                f = xbmcvfs.File(path)
                try:
                    return f.size()
                finally:
                    f.close()
        except Exception:
            pass
        return os.path.getsize(path)

    return _call()


class _VfsReader:
    """Buffered binary reader over xbmcvfs.File.

    Kodi: File.read() returns TEXT (readBytes() is binary), a VFS read may return
    fewer bytes, and seeks are server requests -- reads fill a read-ahead window."""

    BLOCK = 256 * 1024

    def __init__(self, f):
        self._f = f
        self._read = getattr(f, "readBytes", None) or f.read
        self._pos = 0
        self._win = b""
        self._win_start = 0

    def _to_bytes(self, data):
        if data is None:
            # A VFS read may return None (treat as EOF).
            return b""
        if isinstance(data, str):
            return data.encode("utf-8", "surrogateescape")
        return data

    def _fill(self, pos, want):
        """Window covering [pos, pos+want) (one request; BLOCK if possible)."""
        if self._win and self._win_start <= pos \
                and pos + want <= self._win_start + len(self._win):
            return
        try:
            self._f.seek(pos)
        except Exception:
            pass
        self._win = self._to_bytes(self._read(self.BLOCK))
        self._win_start = pos

    def read(self, n=-1):
        out = b""
        if n is None or n < 0:
            while True:
                self._fill(self._pos, 1)
                chunk = self._win[self._pos - self._win_start:]
                if not chunk:
                    break
                out += chunk
                self._pos += len(chunk)
            return out
        while len(out) < n:
            self._fill(self._pos, n - len(out))
            off = self._pos - self._win_start
            if off >= len(self._win):
                break
            chunk = self._win[off:off + (n - len(out))]
            if not chunk:
                break
            out += chunk
            self._pos += len(chunk)
        return out

    def seek(self, pos, whence=0):
        # never touch the file here: the window is filled on the next read
        if whence == 1:
            self._pos += int(pos)
        elif whence == 2:
            self._pos = max(0, self._seek_end() + int(pos))
        else:
            self._pos = max(0, int(pos))
        return self._pos

    def _seek_end(self):
        try:
            self._f.seek(0, 2)
            end = int(self._f.tell())
            self._f.seek(0, 0)
            return end
        except Exception:
            return 0

    def tell(self):
        return self._pos

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _open_bin(path):
    """Binary handle for a media file: open() locally, xbmcvfs.File (wrapped in
    the read-ahead adapter) for a network URL. Imports stay lazy."""
    try:
        import sources
        if sources.is_network_path(path):
            import xbmcvfs
            return _VfsReader(xbmcvfs.File(path))
    except Exception:
        pass
    return open(path, "rb")


def _cache_file():
    try:
        from common import state_dir
        return os.path.join(state_dir(), "metadata.json")
    except Exception:
        return ""


# ---- Embedded cover art ------------------------------------------------------
COVER_MAX = 3             # pictures shown side by side in the INFO modal
COVER_BYTES = 10 << 20   # per-picture cap: a corrupt length must not allocate
COVER_DIR_KEEP = 50       # content-addressed files kept in the cover cache


def _pic_ext(blob):
    """Image extension by magic bytes, or "" for an unknown payload."""
    if blob[:3] == b"\xff\xd8\xff":
        return "jpg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if blob[:2] == b"BM":
        return "bmp"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return ""


def _after_desc(data, pos, wide):
    """Skip a zero-terminated ID3 description at pos -> payload start."""
    if wide:
        end = data.find(b"\x00\x00", pos)
        pos = len(data) if end < 0 else end + 2
    else:
        end = data.find(b"\x00", pos)
        pos = len(data) if end < 0 else end + 1
    # A mid-character match leaves stray zeros; image magic never starts with one.
    while pos < len(data) and data[pos:pos + 1] == b"\x00" and pos < 16:
        pos += 1
    return pos


def _apic_data(data):
    """Payload of an ID3v2.3/v2.4 APIC frame, or b""."""
    if len(data) < 5:
        return b""
    try:
        mend = data.index(b"\x00", 1)
    except ValueError:
        return b""
    return data[_after_desc(data, mend + 2, data[0] in (1, 2)):]


def _pic_frame_data(data):
    """Payload of an ID3v2.2 PIC frame, or b""."""
    if len(data) < 6:
        return b""
    return data[_after_desc(data, 5, data[0] == 1):]


def _id3_frames(path):
    """Raw (id, payload) ID3v2 frames in file order, or [] (max 64 frames)."""
    try:
        size = _size(path)
    except OSError:
        return []
    frames = []
    try:
        with _open_bin(path) as f:
            head = f.read(10)
            if head[:3] != b"ID3":
                return []
            ver, flags = head[3], head[5]
            end = 10 + _synchsafe(head[6:10])
            f.seek(10)
            if flags & 0x40:  # extended header
                ext = f.read(4)
                if len(ext) == 4:
                    esz = _synchsafe(ext) if ver >= 4 else _u32(ext)
                    f.seek(f.tell() + (esz - 4 if ver >= 4 else esz))
            if ver == 2:
                while f.tell() + 6 <= end and len(frames) < 64:
                    fh = f.read(6)
                    if len(fh) < 6 or fh[0] == 0:
                        break
                    sz = int.from_bytes(fh[3:6], "big")
                    if sz <= 0 or sz > COVER_BYTES + 64 \
                            or f.tell() + sz > end:
                        break
                    frames.append((fh[:3], f.read(sz)))
            else:
                while f.tell() + 10 <= end and len(frames) < 64:
                    fh = f.read(10)
                    if len(fh) < 10 or fh[0] == 0:
                        break
                    sz = _synchsafe(fh[4:8]) if ver >= 4 else _u32(fh, 4)
                    if sz <= 0 or sz > COVER_BYTES + 64 \
                            or f.tell() + sz > end:
                        break
                    frames.append((fh[:4], f.read(sz)))
    except Exception:
        pass
    return frames


def _mp3_pictures(path):
    pics = []
    for fid, data in _id3_frames(path):
        if fid == b"APIC":
            blob = _apic_data(data)
        elif fid == b"PIC":
            blob = _pic_frame_data(data)
        else:
            continue
        if not blob or len(blob) > COVER_BYTES:
            continue
        if _pic_ext(blob):
            pics.append(blob)
        if len(pics) >= COVER_MAX:
            break
    return pics


def _mp4_pictures(path):
    """Payloads of the moov > udta > meta > ilst > covr boxes, or []."""
    pics = []
    try:
        size = _size(path)
    except OSError:
        return pics
    try:
        with _open_bin(path) as f:
            moov = _find(f, 0, size, b"moov")
            if not moov:
                return pics
            udta = _find(f, moov[0], moov[1], b"udta")
            meta = _find(f, udta[0], udta[1], b"meta") if udta else None
            if not meta:
                return pics
            # meta is a FullBox (4 bytes version/flags); be tolerant
            ilst = _find(f, meta[0] + 4, meta[1], b"ilst") \
                or _find(f, meta[0], meta[1], b"ilst")
            if not ilst:
                return pics
            for typ, body, box_end in _children(f, ilst[0], ilst[1]):
                if typ != b"covr":
                    continue
                for dtyp, dbody, dend in _children(f, body, box_end):
                    if dtyp != b"data":
                        continue
                    f.seek(dbody)
                    raw = f.read(dend - dbody)
                    # data box: 4 bytes type + 4 bytes locale, then the image
                    if len(raw) > 8 and len(raw) - 8 <= COVER_BYTES:
                        blob = raw[8:]
                        if _pic_ext(blob):
                            pics.append(blob)
                    if len(pics) >= COVER_MAX:
                        break
                if len(pics) >= COVER_MAX:
                    break
    except Exception:
        pass
    return pics


def _flac_picture(data):
    """Payload of a FLAC PICTURE block body, or b""."""
    try:
        if len(data) < 32:
            return b""
        pos = 4
        mlen = int.from_bytes(data[pos:pos + 4], "big")
        pos += 4 + mlen
        if pos + 4 > len(data):
            return b""
        dlen = int.from_bytes(data[pos:pos + 4], "big")
        pos += 4 + dlen + 16  # description + w/h/depth/colors
        if pos + 4 > len(data):
            return b""
        n = int.from_bytes(data[pos:pos + 4], "big")
        pos += 4
        blob = data[pos:pos + n]
        if len(blob) != n or n > COVER_BYTES:
            return b""
        return blob if _pic_ext(blob) else b""
    except Exception:
        return b""


def _flac_pictures(path):
    pics = []
    try:
        with _open_bin(path) as f:
            if f.read(4) != b"fLaC":
                return pics
            for _ in range(64):
                bh = f.read(4)
                if len(bh) < 4:
                    break
                btype, size = bh[0] & 0x7F, int.from_bytes(bh[1:4], "big")
                if btype == 6:
                    if size > COVER_BYTES + 64:
                        f.seek(size, 1)
                        continue
                    blob = _flac_picture(f.read(size))
                    if blob:
                        pics.append(blob)
                    if len(pics) >= COVER_MAX:
                        break
                elif size > (1 << 20):
                    f.seek(size, 1)
                else:
                    f.read(size)
                if bh[0] & 0x80:
                    break
    except Exception:
        pass
    return pics


def _ogg_pictures(path):
    """Payloads of the METADATA_BLOCK_PICTURE Vorbis comments, or []."""
    pics = []
    try:
        with _open_bin(path) as f:
            packets = _ogg_packets(f, limit=4 << 20, max_packets=32)
    except Exception:
        return pics
    import base64
    for pk, _serial in packets:
        if pk[:7] == b"\x03vorbis":
            pos = 7
        elif pk[:8] == b"OpusTags":
            pos = 8
        else:
            continue
        try:
            if len(pk) < pos + 8:
                continue
            vlen = int.from_bytes(pk[pos:pos + 4], "little")
            pos += 4 + vlen
            count = int.from_bytes(pk[pos:pos + 4], "little")
            pos += 4
            for _ in range(count):
                if pos + 4 > len(pk):
                    break
                clen = int.from_bytes(pk[pos:pos + 4], "little")
                pos += 4
                item = pk[pos:pos + clen]
                pos += clen
                if b"=" not in item:
                    continue
                key, val = item.split(b"=", 1)
                if key.strip().upper() != b"METADATA_BLOCK_PICTURE":
                    continue
                try:
                    blob = _flac_picture(base64.b64decode(val))
                except Exception:
                    continue
                if blob:
                    pics.append(blob)
                if len(pics) >= COVER_MAX:
                    return pics
        except Exception:
            continue
    return pics


def _mkv_attachment_pics(f, start, end, pics):
    """Append (name, payload) image attachments in element order."""
    for idv, s, e in _ebml_elements(f, start, end):
        if idv != _ID_ATTACHED_FILE or len(pics) >= _MKV_ATTACH_SCAN:
            continue
        name = mime = ""
        data = b""
        for i2, s2, e2 in _ebml_elements(f, s, e):
            if i2 == _ID_FILE_NAME:
                name = _mkv_str(f, s2, e2)
            elif i2 == _ID_FILE_MIME:
                mime = _mkv_str(f, s2, e2)
            elif i2 == _ID_FILE_DATA:
                if e2 - s2 <= COVER_BYTES:
                    f.seek(s2)
                    data = f.read(e2 - s2)
        if data and mime.lower().startswith("image/") and _pic_ext(data):
            pics.append((name, data))


_MKV_ATTACH_SCAN = 12  # entries scanned: covers sort first afterwards


def _mkv_pictures(path):
    """Image attachments of an MKV/WebM file, cover-named first."""
    pics = []
    try:
        size = _size(path)
    except OSError:
        return pics
    try:
        with _open_bin(path) as f:
            seg = None
            for idv, s, e in _ebml_elements(f, 0, size):
                if idv == _ID_SEGMENT:
                    seg = (s, e)
                    break
            if seg is None:
                return pics
            sseg, eseg = seg
            seg_elem = sseg
            f.seek(max(0, sseg - 12))
            probe = f.read(sseg - max(0, sseg - 12))
            at = probe.rfind(b"\x18\x53\x80\x67")
            if at >= 0:
                seg_elem = max(0, sseg - 12) + at
            seekmap = {}
            found = False
            limit = min(eseg, sseg + _MKV_HEAD_BUDGET)
            for i2, s2, e2 in _ebml_elements(f, sseg, eseg):
                if s2 >= limit:
                    break
                if i2 == _ID_ATTACHMENTS:
                    _mkv_attachment_pics(f, s2, e2, pics)
                    found = True
                    break
                if i2 == _ID_SEEKHEAD:
                    for k, v in _mkv_seek_targets(f, s2, e2).items():
                        seekmap.setdefault(k, v)
            if not found and _ID_ATTACHMENTS in seekmap:
                at = _mkv_jump(f, sseg, seg_elem,
                               seekmap[_ID_ATTACHMENTS], _ID_ATTACHMENTS)
                if at:
                    _mkv_attachment_pics(f, at[0], at[1], pics)
    except Exception:
        pass
    named = [b for n, b in pics if "cover" in n.lower()]
    rest = [b for n, b in pics if "cover" not in n.lower()]
    return (named + rest)[:COVER_MAX]


def _cover_blobs(path):
    """Embedded picture payloads of an audio file (at most COVER_MAX)."""
    try:
        with _open_bin(path) as f:
            head = f.read(16)
    except Exception:
        return []
    if len(head) < 4:
        return []
    if head[:3] == b"ID3" or (head[:1] == b"\xff" and (head[1] & 0xF0) == 0xF0):
        return _mp3_pictures(path)
    if head[:4] == b"fLaC":
        return _flac_pictures(path)
    if head[:4] == b"OggS":
        return _ogg_pictures(path)
    if head[:4] == _EBML_MAGIC:
        return _mkv_pictures(path)
    if head[4:8] == b"ftyp":
        return _mp4_pictures(path)
    return []


def _cover_dir():
    try:
        from common import state_dir
        d = os.path.join(state_dir(), "info-covers")
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return ""


def _cover_index():
    try:
        from common import state_dir
        return os.path.join(state_dir(), "info-covers.json")
    except Exception:
        return ""


def _atomic_bytes(dest, blob):
    import tempfile
    directory = os.path.dirname(dest) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(dest) + ".tmp-",
                               dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _prune_covers(d):
    try:
        files = [os.path.join(d, n) for n in os.listdir(d)]
        files = [p for p in files if os.path.isfile(p)]
        if len(files) <= COVER_DIR_KEEP:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[:len(files) - COVER_DIR_KEEP]:
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception:
        pass


def covers(path):
    """Local cache paths of the file's embedded pictures (at most COVER_MAX).
    Content-addressed (<md5>.<ext>), so a changed tag never serves a stale picture."""
    try:
        from common import cache_key
        key = cache_key(path)
    except Exception:
        return []
    if not key:
        return []
    d = _cover_dir()
    idx = _cover_index()
    if d and idx:
        try:
            import json
            with open(idx, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict) and saved.get("path") == key:
                names = saved.get("files", [])
                if isinstance(names, list) and names:
                    full = [os.path.join(d, n) for n in names]
                    if all(os.path.isfile(p) for p in full):
                        return full
        except Exception:
            pass
    blobs = _cover_blobs(path)
    if not blobs or not d:
        return []
    import hashlib
    names = []
    for blob in blobs:
        name = hashlib.md5(blob).hexdigest() + "." + _pic_ext(blob)
        dest = os.path.join(d, name)
        if not os.path.isfile(dest):
            try:
                _atomic_bytes(dest, blob)
            except Exception:
                pass
        if os.path.isfile(dest):
            names.append(name)
    if idx:
        try:
            from common import write_json
            write_json(idx, {"path": key, "files": names})
        except Exception:
            pass
    _prune_covers(d)
    return [os.path.join(d, n) for n in names]


def covers_cached(path):
    """Cached cover paths for `path` WITHOUT reading the file, or None. Same
    index lookup as `covers` but never extracts. Never raises."""
    try:
        from common import cache_key
        key = cache_key(path)
    except Exception:
        return None
    if not key:
        return None
    d = _cover_dir()
    idx = _cover_index()
    if not (d and idx):
        return None
    try:
        import json
        with open(idx, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict) and saved.get("path") == key:
            names = saved.get("files", [])
            if isinstance(names, list) and names:
                full = [os.path.join(d, n) for n in names]
                if all(os.path.isfile(p) for p in full):
                    return full
    except Exception:
        pass
    return None


def load_cache(path):
    """Metadata cached for `path`, or None (stored by credential-free cache key,
    so no secrets persist in metadata.json)."""
    f = _cache_file()
    if not f or not path:
        return None
    try:
        from common import cache_key
        key = cache_key(path)
    except Exception:
        return None
    try:
        import json
        with open(f, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("path") == key \
                and isinstance(data.get("info"), dict):
            return data["info"]
    except Exception:
        pass
    return None


def store_cache(path, info):
    """Remember the result for the currently playing file. An empty result is
    not cached (load_cache would report "already scanned"). Written atomically."""
    f = _cache_file()
    if not f or not path or not isinstance(info, dict) or not info:
        return
    try:
        from common import cache_key, write_json
        key = cache_key(path)
    except Exception:
        return
    try:
        write_json(f, {"path": key, "info": info})
    except Exception:
        pass


def read_info(path):
    """Tags + technical stream info for MP4/M4A, MKV/WebM, MP3, FLAC, Ogg, WAV,
    AIFF, AAC, ASF, AVI, FLV, MPEG-TS and MPEG-PS; other containers return {}."""
    try:
        with _open_bin(path) as f:
            head = f.read(16)
    except OSError:
        return {}
    ext = os.path.splitext(path)[1].lower()
    if head[:4] == _EBML_MAGIC:
        return _read_mkv(path)
    if head[:16] == _ASF_HEADER:
        return _read_asf(path)
    if head[:3] == b"ID3":
        return _read_aac(path) if ext == ".aac" else _read_mp3(path)
    if head[:1] == b"\xff" and (head[1] & 0xF0) == 0xF0:
        return _read_aac(path) if ext == ".aac" else _read_mp3(path)
    if head[:4] == b"fLaC":
        return _read_flac(path)
    if head[:3] == b"FLV":
        return _read_flv(path)
    if head[:4] == b"OggS":
        return _read_ogg(path)
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return _read_aiff(path)
    if head[:4] == b"RIFF":
        if head[8:12] == b"WAVE":
            return _read_wav(path)
        if head[8:12] == b"AVI ":
            return _read_avi(path)
    if ext in (".ts", ".m2ts", ".mts", ".trp"):
        ts = _read_ts(path)
        if ts:
            return ts
    if ext in (".mpg", ".mpeg", ".m2p", ".vob", ".m1v", ".m2v"):
        return _read_ps(path)
    return _read_mp4(path)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print(p, "->", read_info(p))

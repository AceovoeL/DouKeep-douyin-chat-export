"""Minimal, dependency-free MP4 (ISO-BMFF) box reader.

The chat viewer only ever needs three facts about a video file:

1. **Which codec it really is.** Douyin's CDN labels part of its catalogue as
   MPEG-CENC (``encv`` / ``enca`` sample entries) even though ``tenc`` reports
   ``default_isProtected = 0``, i.e. the sample bytes are plain. The real codec
   hides in the ``frma`` box, so ``encv`` must be unwrapped before anything can
   decide whether a browser is able to play the file.
2. **How long it is.** Used to refuse absurdly long transcodes.
3. **Whether stale CENC labels are still present.** That is what
   ``tools/fix_media_containers.py`` reports on.

Kept free of third-party imports on purpose: this runs inside the request path
(``/media/...``) and inside a maintenance CLI, and neither should need ffmpeg
just to *look* at a file.
"""
import os
import struct
from collections import namedtuple

# Boxes that merely group other boxes. Everything else is a leaf we skip past.
_CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts",
               b"udta", b"mvex", b"moof", b"traf", b"sinf", b"schi")

# Bytes between the start of a sample entry box and its first child box:
#   box header (8) + reserved(6) + data_reference_index(2) [+ 70 visual fields]
#   box header (8) + reserved(6) + data_reference_index(2) [+ 20 audio fields]
# Only the type-specific payload differs between visual and audio entries.
_VISUAL_PREFIX = 8 + 8 + 70
_AUDIO_PREFIX = 8 + 8 + 20

VIDEO_ENTRIES = {
    b"avc1": "h264", b"avc3": "h264", b"avc2": "h264", b"avc4": "h264",
    b"hvc1": "hevc", b"hev1": "hevc",
    b"vp08": "vp8", b"vp09": "vp9",
    b"av01": "av1",
    b"mp4v": "mpeg4",
}
AUDIO_ENTRIES = {
    b"mp4a": "aac", b"ac-3": "ac3", b"ec-3": "eac3", b"Opus": "opus",
    b"alac": "alac", b"twos": "pcm_s16be", b"sowt": "pcm_s16le",
}

# The CENC bookkeeping Douyin leaves behind on already-plain files.
CENC_BOXES = (b"sinf", b"frma", b"schm", b"tenc", b"senc", b"saiz",
              b"saio", b"sgpd", b"sbgp", b"pssh", b"uuid")

# Codecs no Chromium build decodes on its own, and that Windows Media
# Foundation only decodes once the paid "HEVC 视频扩展" is installed.
HARD_TO_PLAY = frozenset({"hevc"})


class SampleEntry(namedtuple("SampleEntry", "fourcc start size")):
    """A ``stsd`` entry: the fourcc plus the byte range of the whole box."""

    __slots__ = ()


def iter_boxes(buf, start=0, end=None):
    """Yield ``(fourcc, box_start, header_len, box_size)`` for each sibling box.

    Tolerates 64-bit sizes (``size == 1``), boxes that run to end-of-file
    (``size == 0``) and truncated tails — a half-written download must not
    raise here, it must simply stop yielding.
    """
    if end is None:
        end = len(buf)
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", buf, pos)[0]
        fourcc = bytes(buf[pos + 4:pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield fourcc, pos, header, size
        pos += size


def read_probe_bytes(path, head_bytes=4 * 1024 * 1024):
    """Read enough of ``path`` to walk its box tree.

    Douyin clips keep ``moov`` at the front and are a few megabytes, so the
    first slice is normally the whole file. When the slice cuts a box in half
    we fall back to reading everything, which is why this returns bytes rather
    than a file object.
    """
    total = os.path.getsize(path)
    with open(path, "rb") as handle:
        head = handle.read(min(total, head_bytes))
        if len(head) < total and not _boxes_are_complete(head):
            handle.seek(0)
            head = handle.read()
    return head


def _boxes_are_complete(buf):
    """True when ``buf`` ends exactly on a box boundary."""
    pos = 0
    end = len(buf)
    while pos + 8 <= end:
        size = struct.unpack_from(">I", buf, pos)[0]
        if size == 1:
            if pos + 16 > end:
                return False
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
        elif size == 0:
            return pos + 8 == end
        if size < 8 or pos + size > end:
            return False
        pos += size
    return pos == end


def sample_entries(buf, start=0, end=None):
    """Return every ``stsd`` sample entry in the file, in track order."""
    if end is None:
        end = len(buf)
    found = []
    for fourcc, pos, header, size in iter_boxes(buf, start, end):
        if fourcc in _CONTAINERS:
            found.extend(sample_entries(buf, pos + header, pos + size))
        elif fourcc == b"stsd":
            found.extend(_entries_in_stsd(buf, pos, header, size))
    return found


def _entries_in_stsd(buf, stsd_start, stsd_header, stsd_size):
    body = stsd_start + stsd_header
    limit = stsd_start + stsd_size
    if body + 8 > limit:
        return []
    count = struct.unpack_from(">I", buf, body + 4)[0]
    pos = body + 8
    out = []
    for _ in range(count):
        if pos + 8 > limit:
            break
        size = struct.unpack_from(">I", buf, pos)[0]
        if size < 8 or pos + size > limit:
            break
        out.append(SampleEntry(bytes(buf[pos + 4:pos + 8]), pos, size))
        pos += size
    return out


def entry_children_start(entry):
    """Offset of a sample entry's first child box, or ``None`` if unknown."""
    if entry.fourcc in VIDEO_ENTRIES or entry.fourcc == b"encv":
        return entry.start + _VISUAL_PREFIX
    if entry.fourcc in AUDIO_ENTRIES or entry.fourcc == b"enca":
        return entry.start + _AUDIO_PREFIX
    return None


def entry_child_boxes(buf, entry):
    """Yield the child boxes of a sample entry (``sinf``/``frma``/``esds``…)."""
    base = entry_children_start(entry)
    if base is None or base >= entry.start + entry.size:
        return
    yield from iter_boxes(buf, base, entry.start + entry.size)


def unwrap_fourcc(buf, entry):
    """Resolve ``encv``/``enca`` to the codec recorded in ``frma``.

    Returns ``entry.fourcc`` unchanged for entries that are not CENC-framed,
    and ``None`` when a CENC entry carries no ``frma`` at all (nothing sensible
    can be concluded about such a file).
    """
    if entry.fourcc not in (b"encv", b"enca"):
        return entry.fourcc
    for fourcc, pos, header, size in entry_child_boxes(buf, entry):
        if fourcc == b"sinf":
            for inner, ipos, iheader, isize in iter_boxes(buf, pos + header, pos + size):
                if inner == b"frma":
                    value = bytes(buf[ipos + iheader:ipos + iheader + 4])
                    return value or None
    return None


def codec_name(fourcc):
    """Map a sample-entry fourcc to an ffmpeg-style codec name."""
    if fourcc is None:
        return None
    if fourcc in VIDEO_ENTRIES:
        return VIDEO_ENTRIES[fourcc]
    if fourcc in AUDIO_ENTRIES:
        return AUDIO_ENTRIES[fourcc]
    return fourcc.decode("latin-1", "replace").strip() or None


def video_codec(path):
    """Real video codec name (``h264`` / ``hevc`` / …) or ``None``."""
    buf = read_probe_bytes(path)
    for entry in sample_entries(buf):
        if entry.fourcc == b"encv" or entry.fourcc in VIDEO_ENTRIES:
            return codec_name(unwrap_fourcc(buf, entry))
    return None


def audio_codec(path):
    buf = read_probe_bytes(path)
    for entry in sample_entries(buf):
        if entry.fourcc == b"enca" or entry.fourcc in AUDIO_ENTRIES:
            return codec_name(unwrap_fourcc(buf, entry))
    return None


def duration_seconds(path):
    """Movie duration from ``mvhd``, or ``None`` when it cannot be read."""
    buf = read_probe_bytes(path)
    for fourcc, pos, header, size in iter_boxes(buf):
        if fourcc != b"moov":
            continue
        for inner, ipos, iheader, isize in iter_boxes(buf, pos + header, pos + size):
            if inner != b"mvhd":
                continue
            body = ipos + iheader
            if body + 20 > ipos + isize:
                return None
            version = buf[body]
            if version == 1:
                if body + 32 > ipos + isize:
                    return None
                timescale = struct.unpack_from(">I", buf, body + 20)[0]
                duration = struct.unpack_from(">Q", buf, body + 24)[0]
            else:
                timescale = struct.unpack_from(">I", buf, body + 12)[0]
                duration = struct.unpack_from(">I", buf, body + 16)[0]
            if not timescale:
                return None
            return duration / timescale
    return None


def cenc_labels(path):
    """Sample-entry fourccs plus stale CENC box names still in the file."""

    def collect(buf, start, end, acc, boxes):
        for fourcc, pos, header, size in iter_boxes(buf, start, end):
            if fourcc in CENC_BOXES:
                boxes.add(fourcc.decode("latin-1"))
            if fourcc in _CONTAINERS:
                collect(buf, pos + header, pos + size, acc, boxes)
            elif fourcc == b"stsd":
                for entry in _entries_in_stsd(buf, pos, header, size):
                    acc.append(entry.fourcc.decode("latin-1"))
                    child_start = entry_children_start(entry)
                    if child_start is not None and child_start < entry.start + entry.size:
                        collect(buf, child_start, entry.start + entry.size, acc, boxes)

    buf = read_probe_bytes(path)
    entries, boxes = [], set()
    collect(buf, 0, len(buf), entries, boxes)
    return {"entries": entries, "boxes": sorted(boxes)}


def needs_container_fix(path):
    """True when a file still carries Douyin's CENC labels."""
    labels = cenc_labels(path)
    return any(name in ("encv", "enca") for name in labels["entries"])

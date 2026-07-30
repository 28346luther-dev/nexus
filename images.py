"""Image sniffing for uploads. Stdlib only.

We never trust the Content-Type or the filename a browser sends — the format is
decided by looking at the actual bytes. Only raster formats are allowed:
SVG is deliberately excluded because it can carry <script> and would run in the
context of the site when opened.
"""

import struct

# Extension and MIME are derived from the sniffed format, never from the client.
FORMATS = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

MAX_UPLOAD = 8 * 1024 * 1024        # 8 MB
MAX_STICKER = 1 * 1024 * 1024       # 1 MB — stickers render small
MAX_AVATAR = 2 * 1024 * 1024        # 2 MB — avatars render smaller still


def sniff(data):
    """Return 'png' | 'jpg' | 'gif' | 'webp', or None if it isn't one of those."""
    if len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def dimensions(data, kind):
    """Best-effort (width, height); (None, None) when it cannot be determined.

    Sending these to the browser lets it reserve the right space before the
    image loads, so the message list doesn't jump around.
    """
    try:
        if kind == "png":
            # IHDR is always the first chunk: 8-byte signature, 4 length,
            # 4 type, then width and height as big-endian uint32.
            return struct.unpack(">II", data[16:24])
        if kind == "gif":
            return struct.unpack("<HH", data[6:10])
        if kind == "webp":
            return _webp_dimensions(data)
        if kind == "jpg":
            return _jpeg_dimensions(data)
    except (struct.error, IndexError, ValueError):
        pass
    return (None, None)


def _webp_dimensions(data):
    chunk = data[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return (w, h)
    if chunk == b"VP8 ":
        return struct.unpack("<HH", data[26:30])
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    raise ValueError("unknown webp chunk")


# Start-of-frame markers carry the dimensions; DHT/DQT and friends do not.
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_dimensions(data):
    i, end = 2, len(data)
    while i + 9 < end:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in _SOF:
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return (width, height)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        segment = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + segment
    raise ValueError("no SOF segment")

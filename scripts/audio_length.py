"""Track length from the first bytes of an original file plus its total size, before any full transfer.

WAV, AIFF and FLAC headers give the exact length. MP3 uses a Xing/Info/VBRI frame count when present, otherwise
the first frame's bitrate across the file size, which is accurate for constant bitrate within a second or two.
Each reader returns seconds, None when the format is not recognised, or ("need", offset) when an ID3 tag pushes
the first MP3 frame beyond the bytes read, so the caller can fetch a second range from that offset.
"""
import struct

HEAD_BYTES = 65536

_MP3_BITRATES = {  # (MPEG version 1?, layer 3) kbps tables
    True: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    False: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MP3_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _wav(head, total):
    position = 12
    rate = None
    while position + 8 <= len(head):
        chunk, size = head[position:position + 4], struct.unpack("<I", head[position + 4:position + 8])[0]
        if chunk == b"fmt ":
            rate = struct.unpack("<I", head[position + 16:position + 20])[0]  # bytes per second
        elif chunk == b"data" and rate:
            data = size if 0 < size < 0xFFFFFFFF and position + 8 + size <= total else total - position - 8
            return data / rate
        position += 8 + size + (size & 1)
    return None


def _ieee_extended(raw):
    exponent, mantissa = struct.unpack(">HQ", raw)
    sign = -1 if exponent & 0x8000 else 1
    exponent &= 0x7FFF
    return 0.0 if exponent == 0 and mantissa == 0 else sign * mantissa * 2.0 ** (exponent - 16383 - 63)


def _aiff(head, total):
    position = 12
    while position + 8 <= len(head):
        chunk, size = head[position:position + 4], struct.unpack(">I", head[position + 4:position + 8])[0]
        if chunk == b"COMM" and position + 26 <= len(head):
            frames = struct.unpack(">I", head[position + 10:position + 14])[0]
            rate = _ieee_extended(head[position + 16:position + 26])
            return frames / rate if rate else None
        position += 8 + size + (size & 1)
    return None


def _flac(head, total):
    if len(head) < 26 or head[4] & 0x7F != 0:  # STREAMINFO is always the first metadata block
        return None
    info = int.from_bytes(head[18:26], "big")
    rate, samples = info >> 44, info & ((1 << 36) - 1)
    return samples / rate if rate and samples else None


def _mp3(head, total, offset=0):
    position = 0
    if head[:3] == b"ID3" and offset == 0:
        tag = 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F))
        if tag + 4 > len(head):
            return ("need", tag)
        position = tag
    while position + 4 <= len(head):
        if head[position] == 0xFF and head[position + 1] & 0xE0 == 0xE0:
            b1, b2 = head[position + 1], head[position + 2]
            version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
            index, rate_index = b2 >> 4, (b2 >> 2) & 3
            if version != 1 and layer == 1 and 0 < index < 15 and rate_index < 3:
                mpeg1 = version == 3
                rate = _MP3_RATES[version][rate_index]
                per_frame = 1152 if mpeg1 else 576
                mono = (head[position + 3] >> 6) == 3
                side = (17 if mono else 32) if mpeg1 else (9 if mono else 17)
                xing = head[position + 4 + side:position + 8 + side]
                if xing in (b"Xing", b"Info") and head[position + 11 + side] & 1:
                    frames = struct.unpack(">I", head[position + 12 + side:position + 16 + side])[0]
                    return frames * per_frame / rate
                if head[position + 36:position + 40] == b"VBRI":
                    frames = struct.unpack(">I", head[position + 50:position + 54])[0]
                    return frames * per_frame / rate
                audio = total - offset - position
                return audio * 8 / (_MP3_BITRATES[mpeg1][index] * 1000)
        position += 1
    return None


_CONTAINERS = ((b"RIFF", "wav"), (b"FORM", "aiff"), (b"fLaC", "flac"), (b"ID3", "mp3"), (b"\xff\xfb", "mp3"),
               (b"\xff\xf3", "mp3"), (b"\xff\xf2", "mp3"))


def container(head):
    """File type from its first bytes: wav, aiff, flac, mp3 or None."""
    for magic, kind in _CONTAINERS:
        if head.startswith(magic):
            return kind
    return "m4a" if head[4:8] == b"ftyp" else None


def length_from_header(head, total, offset=0):
    """Seconds, None, or ("need", offset) for a second read. offset: where `head` starts within the file."""
    if offset:
        return _mp3(head, total, offset)
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return _wav(head, total)
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return _aiff(head, total)
    if head[:4] == b"fLaC":
        return _flac(head, total)
    return _mp3(head, total)

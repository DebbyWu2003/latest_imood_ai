"""Downstream framed protocol: type 1B | length 4B (big endian) | payload.

BEGIN/AUDIO/END client -> receiver, STATUS receiver -> client.
BEGIN   JSON {session, utterance, voice, sample_rate, emotion, text}
AUDIO   raw PCM16 LE mono 16 kHz
END     empty
STATUS  JSON {utterance, state}
"""
import struct

BEGIN, AUDIO, END, STATUS = 1, 2, 3, 4
HEADER = struct.Struct(">BI")
MAX_PAYLOAD = 16 * 1024 * 1024


def pack(ftype, payload=b""):
    return HEADER.pack(ftype, len(payload)) + payload


def unpack_header(raw):
    ftype, n = HEADER.unpack(raw)
    if ftype not in (BEGIN, AUDIO, END, STATUS) or n > MAX_PAYLOAD:
        raise ValueError(f"bad frame header type={ftype} length={n}")
    return ftype, n

"""Strict bencode decoder for .torrent metadata parsing.

Reads ONLY the structure needed to list files (name/length/path); piece
hashes and tracker URLs are ignored. Strict caps (input size, nesting
depth) so a malicious .torrent cannot blow memory or recursion.
No third-party dependencies.
"""

from __future__ import annotations

MAX_TORRENT_BYTES = 5 * 1024 * 1024  # .torrent metainfo is kilobytes; 5 MB is generous
MAX_DEPTH = 16


class BencodeError(ValueError):
    pass


def decode(data: bytes):
    """Decode bencode bytes; returns int | bytes | list | dict."""
    if not isinstance(data, (bytes, bytearray)):
        raise BencodeError("input must be bytes")
    if len(data) > MAX_TORRENT_BYTES:
        raise BencodeError(f"torrent file too large ({len(data)} bytes)")
    if not data:
        raise BencodeError("empty torrent file")
    value, pos = _decode_next(bytes(data), 0, 0)
    if pos != len(data):
        raise BencodeError("trailing garbage after bencode value")
    return value


def _decode_next(data: bytes, pos: int, depth: int):
    if depth > MAX_DEPTH:
        raise BencodeError("nesting too deep")
    if pos >= len(data):
        raise BencodeError("truncated input")
    token = data[pos:pos + 1]
    if token == b"i":
        try:
            end = data.index(b"e", pos)
        except ValueError:
            raise BencodeError("unterminated integer")
        try:
            return int(data[pos + 1:end]), end + 1
        except ValueError:
            raise BencodeError("invalid integer")
    if token == b"l":
        out, pos = [], pos + 1
        while data[pos:pos + 1] != b"e":
            item, pos = _decode_next(data, pos, depth + 1)
            out.append(item)
        return out, pos + 1
    if token == b"d":
        out, pos = {}, pos + 1
        while data[pos:pos + 1] != b"e":
            key, pos = _decode_next(data, pos, depth + 1)
            if not isinstance(key, bytes):
                raise BencodeError("dict key must be a byte string")
            val, pos = _decode_next(data, pos, depth + 1)
            out[key] = val
        return out, pos + 1
    if token.isdigit():
        try:
            colon = data.index(b":", pos)
        except ValueError:
            raise BencodeError("unterminated string length")
        try:
            length = int(data[pos:colon])
        except ValueError:
            raise BencodeError("invalid string length")
        if length < 0 or colon + 1 + length > len(data):
            raise BencodeError("string length out of bounds")
        return data[colon + 1:colon + 1 + length], colon + 1 + length
    raise BencodeError(f"invalid token at offset {pos}")


def parse_torrent_info(data: bytes) -> dict:
    """Extract {name, total_size, files:[{index, path, size}]} from .torrent bytes.

    index is 1-based in file-list order (matches aria2c --select-file).
    path is display-only (joined with '/'); NEVER use it as a filesystem
    path without jailing + sanitizing. Sizes are validated non-negative ints.
    """
    top = decode(data)
    if not isinstance(top, dict) or not isinstance(top.get(b"info"), dict):
        raise BencodeError("not a torrent file (missing info dict)")
    info = top[b"info"]

    def _text(raw) -> str:
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        raise BencodeError("torrent name/path must be byte strings")

    name = _text(info.get(b"name", b"unnamed"))
    files = []
    if b"files" in info:
        entries = info[b"files"]
        if not isinstance(entries, list) or not entries:
            raise BencodeError("torrent has an empty file list")
        total = 0
        for i, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                raise BencodeError(f"file entry {i} malformed")
            length = entry.get(b"length")
            parts = entry.get(b"path")
            if not isinstance(length, int) or length < 0:
                raise BencodeError(f"file entry {i} has invalid length")
            if not isinstance(parts, list) or not parts or not all(isinstance(p, bytes) for p in parts):
                raise BencodeError(f"file entry {i} has invalid path")
            # Reject empty segments here; '..' is caught later by jailing (fail closed).
            if any(len(p) == 0 for p in parts):
                raise BencodeError(f"file entry {i} has an empty path segment")
            files.append({"index": i, "path": "/".join(_text(p) for p in parts), "size": length})
            total += length
    else:
        length = info.get(b"length")
        if not isinstance(length, int) or length < 0:
            raise BencodeError("single-file torrent has invalid length")
        files = [{"index": 1, "path": name, "size": length}]
        total = length

    if total <= 0:
        raise BencodeError("torrent contains no data")
    return {"name": name, "total_size": total, "files": files}

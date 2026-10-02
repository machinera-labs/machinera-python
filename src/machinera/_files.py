from __future__ import annotations

import io
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

from ._contract import SUPPORTED_MEDIA_SUFFIXES

FileContent = str | os.PathLike[str] | bytes | BinaryIO
FileInput = (
    FileContent
    | tuple[str | None, FileContent]
    | tuple[str | None, FileContent, str | None]
    | tuple[str | None, FileContent, str | None, Mapping[str, str]]
)
_MIME_SUFFIXES = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "video/mp4": "mp4",
    "audio/webm": "webm",
    "video/webm": "webm",
}
_RESERVED = {
    "authorization",
    "host",
    "content-length",
    "content-type",
    "idempotency-key",
    "transfer-encoding",
    "x-content-md5",
}


def printable(value: str) -> bool:
    return all(32 <= ord(c) <= 126 for c in value)


def header_name(name: object) -> bool:
    return isinstance(name, str) and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is not None


def validate_headers(headers: Mapping[str, str], *, part: bool = False) -> dict[str, str]:
    reserved = _RESERVED | (
        {
            "content-disposition",
            "content-transfer-encoding",
            "cookie",
            "cookie2",
            "proxy-authorization",
            "connection",
            "trailer",
        }
        if part
        else set()
    )
    result = {}
    for name, value in headers.items():
        if not header_name(name):
            raise ValueError("Invalid header name")
        if not isinstance(value, str) or not printable(value):
            raise ValueError("Header values must contain only printable ASCII")
        if name.lower() in reserved:
            raise ValueError("Cannot override an SDK-owned or sensitive header")
        result[name.lower()] = value
    return result


def unpack_file(
    file: FileInput, filename: str | None, content_type: str | None
) -> tuple[FileContent, str | None, str | None, dict[str, str]]:
    headers = {}
    if isinstance(file, tuple):
        if not 2 <= len(file) <= 4:
            raise ValueError("File tuples require two, three, or four items")
        name, content = file[:2]
        media_type = file[2] if len(file) >= 3 else None
        if filename is not None and name is not None and filename != name:
            raise ValueError("Conflicting filename metadata")
        if content_type is not None and media_type is not None and content_type != media_type:
            raise ValueError("Conflicting content_type metadata")
        filename = filename if filename is not None else name
        content_type = content_type if content_type is not None else media_type
        if len(file) == 4:
            headers = validate_headers(file[3], part=True)
    else:
        content = file
    if filename is None:
        candidate = (
            content if isinstance(content, (str, os.PathLike)) else getattr(content, "name", None)
        )
        if isinstance(candidate, (str, os.PathLike)):
            basename = Path(candidate).name
            if Path(basename).suffix.lower().lstrip(".") in SUPPORTED_MEDIA_SUFFIXES:
                filename = basename
    if filename is not None:
        if not isinstance(filename, str):
            raise TypeError("filename must be a string")
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in '"\\' for c in filename):
            raise ValueError("Invalid filename")
        filename = Path(filename).name
        if Path(filename).suffix.lower().lstrip(".") not in SUPPORTED_MEDIA_SUFFIXES:
            raise metadata_error()
    if content_type is not None:
        if not isinstance(content_type, str):
            raise TypeError("content_type must be a string")
        if not content_type or not printable(content_type):
            raise ValueError("Invalid content_type")
    return content, filename, content_type, headers


def metadata_error() -> ValueError:
    return ValueError(
        "Supply filename=/content_type= or tuple metadata for a supported media file; "
        "accepted suffixes: " + ", ".join(sorted(SUPPORTED_MEDIA_SUFFIXES))
    )


def resolve_name(filename: str | None, content_type: str | None, prefix: bytes) -> str:
    if filename is not None:
        return filename
    suffix = (
        _MIME_SUFFIXES.get(content_type.split(";", 1)[0].strip().lower())
        if content_type is not None
        else sniff(prefix)
    )
    if suffix not in SUPPORTED_MEDIA_SUFFIXES:
        raise metadata_error()
    return f"upload.{suffix}"


def sniff(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"fLaC"):
        return "flac"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"ID3"):
        return "mp3"
    if len(data) >= 4 and data[0] == 255 and data[1] & 224 == 224:
        if (
            data[1] & 24 != 8
            and data[1] & 6 != 0
            and data[2] >> 4 not in (0, 15)
            and data[2] & 12 != 12
            and data[3] & 3 != 2
        ):
            return "mp3"
    if len(data) >= 16 and data[4:8] == b"ftyp":
        size = int.from_bytes(data[:4], "big")
        if 16 <= size <= len(data) and size % 4 == 0:
            brands = [data[8:12]] + [data[i : i + 4] for i in range(16, size, 4)]
            if brands[0] == b"M4A ":
                return "m4a"
            if brands[0] in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1"}:
                return "m4a" if b"M4A " in brands else "mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        size, offset = _vint(data, 4)
        end = offset + size
        if size >= 0 and end <= len(data):
            while offset < end:
                start = offset
                _, offset = _vint(data, offset)
                element = data[start:offset]
                length, offset = _vint(data, offset)
                if length < 0 or offset + length > end:
                    break
                if element == b"\x42\x82" and data[offset : offset + length] == b"webm":
                    return "webm"
                offset += length
    return None


def _vint(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data) or data[offset] == 0:
        return -1, len(data)
    width = 9 - data[offset].bit_length()
    if offset + width > len(data):
        return -1, len(data)
    value = int.from_bytes(data[offset : offset + width], "big") & ((1 << (7 * width)) - 1)
    return value, offset + width


def open_file(file: FileContent) -> tuple[BinaryIO, bool]:
    owned = isinstance(file, (str, os.PathLike, bytes))
    source = (
        open(file, "rb")
        if isinstance(file, (str, os.PathLike))
        else io.BytesIO(file)
        if isinstance(file, bytes)
        else file
    )
    if not all(
        callable(getattr(source, name, None)) for name in ("read", "seek", "tell", "seekable")
    ):
        raise TypeError("A path, bytes, or seekable binary handle is required")
    return source, owned

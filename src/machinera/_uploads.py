from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Literal
from xml.etree import ElementTree

import httpx

from ._exceptions import APIResponseValidationError
from ._files import header_name, printable
from ._multipart import Multipart

UploadPhase = Literal["upload_init", "upload_put", "submit", "poll"]


def initialization_key(operation_key: str) -> str:
    return hashlib.sha256(("upload-init:" + operation_key).encode("ascii")).hexdigest()


def descriptor(body: Multipart) -> dict[str, object]:
    suffix = PurePath(body.filename or "").suffix.lower()
    content_type = body.content_type or {
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".mpga": "audio/mpeg",
        ".mpeg": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "video/mp4",
        ".webm": "audio/webm",
    }.get(suffix, "application/octet-stream")
    return {
        "size_bytes": body.size,
        "content_type": content_type,
        "content_md5": body.content_md5,
        "sha256": body.sha256,
    }


@dataclass(frozen=True)
class Grant:
    state: str
    expires_at: float
    upload_expires_at: float
    limits: dict[str, Any] = field(repr=False)
    put_url: str | None = field(repr=False)
    headers: dict[str, str] = field(repr=False)

    @classmethod
    def parse(cls, data: dict[str, Any], expected: dict[str, object], status_code: int) -> Grant:
        invalid = APIResponseValidationError(
            "Invalid upload grant", status_code=status_code, retryable=False
        )
        state = data.get("state")
        if state not in ("pending", "admitting", "bound", "expired", "reclaimed"):
            raise invalid
        for name in ("expires_at", "upload_expires_at"):
            value = data.get(name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise invalid
        limits = data.get("limits")
        if (
            not isinstance(limits, dict)
            or any(
                type(limits.get(name)) is not int or limits[name] <= 0
                for name in (
                    "max_upload_bytes",
                    "sync_inline_body_bytes",
                    "async_inline_body_bytes",
                    "put_ttl_seconds",
                    "upload_window_seconds",
                    "retention_max_seconds",
                )
            )
            or not isinstance(limits.get("policy_revision"), str)
        ):
            raise invalid
        url: str | None = None
        headers: dict[str, str] = {}
        if state == "pending":
            url = data.get("put_url")
            if not isinstance(url, str):
                raise invalid
            try:
                parsed = httpx.URL(url)
            except httpx.InvalidURL:
                raise invalid from None
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.host
                or parsed.userinfo
                or parsed.fragment
            ):
                raise invalid
            supplied = data.get("required_headers")
            if data.get("method") != "PUT" or not isinstance(supplied, dict):
                raise invalid
            for name, value in supplied.items():
                if (
                    not header_name(name)
                    or not isinstance(value, str)
                    or not printable(value)
                    or name.lower()
                    in {
                        "authorization",
                        "cookie",
                        "cookie2",
                        "proxy-authorization",
                        "user-agent",
                        "host",
                        "transfer-encoding",
                    }
                    or name.lower() in headers
                ):
                    raise invalid
                headers[name.lower()] = value
            if any(
                headers.get(name) != value
                for name, value in {
                    "content-length": str(expected["size_bytes"]),
                    "content-md5": expected["content_md5"],
                    "content-type": expected["content_type"],
                    "if-none-match": "*",
                }.items()
            ):
                raise invalid
        return cls(state, data["expires_at"], data["upload_expires_at"], dict(limits), url, headers)


def storage_code(content: bytes) -> str | None:
    if len(content) > 65536 or b"<!" in content:
        return None
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "Code":
            value = element.text
            return value if value and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,127}", value) else None
    return None

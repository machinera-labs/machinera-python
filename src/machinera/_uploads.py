from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Literal
from xml.etree import ElementTree

import httpx

from ._contract import UPLOAD_GRANT_STATES
from ._exceptions import invalid_response
from ._files import _SENSITIVE_HEADERS, _SUFFIX_MIME_TYPES, valid_header
from ._multipart import Multipart

UploadPhase = Literal["upload_init", "upload_put", "submit", "poll"]


def initialization_key(operation_key: str) -> str:
    return hashlib.sha256(("upload-init:" + operation_key).encode("ascii")).hexdigest()


def replacement_key(operation_key: str, upload_id: str) -> str:
    value = f"upload-replacement:{len(operation_key)}:{operation_key}:{upload_id}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def descriptor(body: Multipart) -> dict[str, object]:
    suffix = PurePath(body.filename or "").suffix.lower()
    content_type = body.content_type or _SUFFIX_MIME_TYPES.get(suffix, "application/octet-stream")
    return {
        "size_bytes": body.size,
        "content_type": content_type,
        "content_md5": body.content_md5,
        "sha256": body.sha256,
    }


@dataclass(frozen=True)
class Grant:
    state: str
    expires_at: int
    upload_deadline: int
    submit_expires_at: int | None
    limits: dict[str, Any] = field(repr=False)
    put_url: str | None = field(repr=False)
    headers: dict[str, str] = field(repr=False)

    @classmethod
    def parse(cls, data: dict[str, Any], expected: dict[str, object], status_code: int) -> Grant:
        invalid = invalid_response("Invalid upload grant", status_code=status_code)
        state = data.get("state")
        if state not in UPLOAD_GRANT_STATES:
            raise invalid
        for name in ("expires_at", "upload_deadline", "submit_expires_at"):
            if name not in data:
                raise invalid
            value = data[name]
            if name == "submit_expires_at" and value is None:
                continue
            if type(value) is not int or value <= 0:
                raise invalid
        if state == "pending" and data["expires_at"] > data["upload_deadline"]:
            raise invalid
        if data["submit_expires_at"] is not None and (
            data["submit_expires_at"] > data["upload_deadline"]
            or (
                state == "pending"
                and any(name in data for name in ("put_url", "method", "required_headers"))
            )
        ):
            raise invalid
        limits = data.get("limits")
        if not isinstance(limits, dict) or any(
            type(limits.get(name)) is not int or limits[name] <= 0
            for name in (
                "max_upload_bytes",
                "sync_inline_body_bytes",
                "async_inline_body_bytes",
                "put_ttl_seconds",
                "submit_grace_seconds",
                "retention_max_seconds",
            )
        ):
            raise invalid
        url: str | None = None
        headers: dict[str, str] = {}
        if state == "pending" and any(
            name in data for name in ("put_url", "method", "required_headers")
        ):
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
                try:
                    valid_header(name, value, _SENSITIVE_HEADERS | {"user-agent"})
                except ValueError:
                    raise invalid from None
                if name.lower() in headers:
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
        return cls(
            state,
            data["expires_at"],
            data["upload_deadline"],
            data["submit_expires_at"],
            dict(limits),
            url,
            headers,
        )


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

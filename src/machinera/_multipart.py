from __future__ import annotations

import base64
import hashlib
import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import BinaryIO, TypeVar

import httpx

from ._exceptions import DeadlineExceededError, IntegrityError
from ._files import resolve_name

_CHUNK = 64 * 1024
_T = TypeVar("_T")


class Multipart(httpx.SyncByteStream):
    def __init__(
        self,
        source: BinaryIO,
        fields: dict[str, str],
        filename: str | None,
        check: Callable[[], float],
        max_body_bytes: int,
        content_type: str | None = None,
        part_headers: dict[str, str] | None = None,
        on_staged: Callable[[], None] = lambda: None,
        force_staged: bool = False,
    ) -> None:
        self.on_staged = on_staged
        self.staged = force_staged
        self.source = source
        self.fields = fields
        self.filename = filename
        self.content_type = content_type
        self.part_headers = part_headers or {}
        self.max_body_bytes = max_body_bytes
        self.check = check
        self.lock = threading.Lock()
        self.aborted = threading.Event()
        self.read_idle = threading.Event()
        self.read_idle.set()
        self.offset: int | None = None

    def prepare(self) -> None:
        with self._reading():
            if not self._access(self.source.seekable):
                raise ValueError("A seekable binary file is required")
            self.offset = self._access(self.source.tell)
            leading = b""
            if self.filename is None and self.content_type is None:
                leading = self._access(lambda: self.source.read(4096))
                if not isinstance(leading, bytes):
                    raise TypeError("A binary file is required")
                initial = self.offset
                self._access(lambda: self.source.seek(initial))
            self.filename = resolve_name(self.filename, self.content_type, leading)
            for value in self.fields.values():
                if any(ord(c) < 32 or 127 <= ord(c) <= 159 or c == '"' for c in value):
                    raise ValueError("Multipart field values cannot contain controls or quotes")
            boundary = "machinera-" + secrets.token_hex(24)
            marker = boundary.encode()
            if any(boundary in value for value in self.fields.values()):
                raise ValueError("Multipart boundary occurs in a field value; start a new call")
            prefix = b""
            for name, value in self.fields.items():
                prefix += (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode()
            self.prefix = (
                prefix
                + (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                    f'filename="{self.filename}"\r\n'
                    f"Content-Type: {self.content_type or 'application/octet-stream'}\r\n"
                    + "".join(f"{name}: {value}\r\n" for name, value in self.part_headers.items())
                    + "\r\n"
                ).encode()
            )
            self.suffix = f"\r\n--{boundary}--\r\n".encode()
            self.digests: list[tuple[int, bytes]] = []
            digest = hashlib.md5(self.prefix, usedforsecurity=False)
            self._access(lambda: self.source.seek(0, 2))
            self.size = self._access(self.source.tell) - self.offset
            offset = self.offset
            self._access(lambda: self.source.seek(offset))
            if self.size < 0:
                raise ValueError("File offset is beyond the end of the file")
            self.staged = self.staged or (
                len(self.prefix) + self.size + len(self.suffix) > self.max_body_bytes
            )
            if self.staged:
                self.on_staged()
            raw_md5 = hashlib.md5(usedforsecurity=False)
            raw_sha256 = hashlib.sha256()
            total = 0
            tail = b""
            while True:
                chunk = self._access(lambda: self.source.read(_CHUNK))
                if not isinstance(chunk, bytes):
                    raise TypeError("A binary file is required")
                if not chunk:
                    break
                if not self.staged and marker in tail + chunk:
                    raise ValueError("Multipart boundary occurs in the file; start a new call")
                tail = (tail + chunk)[-len(marker) + 1 :]
                total += len(chunk)
                if total > self.size:
                    raise IntegrityError("File size changed during the operation")
                if not self.staged:
                    self.digests.append((len(chunk), hashlib.sha256(chunk).digest()))
                raw_md5.update(chunk)
                raw_sha256.update(chunk)
                digest.update(chunk)
            if total != self.size:
                raise IntegrityError("File size changed during the operation")
            self.content_md5 = base64.b64encode(raw_md5.digest()).decode("ascii")
            self.sha256 = raw_sha256.hexdigest()
            digest.update(self.suffix)
            self.headers = {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(self.prefix) + self.size + len(self.suffix)),
                "X-Content-MD5": digest.hexdigest(),
            }

    def abort(self) -> None:
        self.aborted.set()

    def _check_abort(self) -> None:
        if self.aborted.is_set():
            raise DeadlineExceededError("Request body cancelled")

    def _access(self, action: Callable[[], _T]) -> _T:
        self._check_abort()
        self.check()
        self._check_abort()
        result = action()
        self._check_abort()
        self.check()
        return result

    @contextmanager
    def _reading(self) -> Iterator[None]:
        with self.lock:
            self.read_idle.clear()
            try:
                self._check_abort()
                yield
            finally:
                try:
                    # An aborted read must not start another operation on the caller's file.
                    if self.offset is not None and not self.aborted.is_set():
                        self.source.seek(self.offset)
                    self._check_abort()
                finally:
                    self.read_idle.set()

    def _read(self, offset: int, length: int) -> bytes:
        with self._reading():
            assert self.offset is not None
            self._access(lambda: self.source.seek(0, 2))
            size = self._access(self.source.tell)
            if size != self.offset + self.size:
                raise IntegrityError("File size changed during the operation")
            self._access(lambda: self.source.seek(offset))
            return self._access(lambda: self.source.read(length)) if length else b""

    def __iter__(self) -> Iterator[bytes]:
        assert self.offset is not None
        self._read(self.offset, 0)
        yield self.prefix
        offset = self.offset
        for length, expected in self.digests:
            chunk = self._read(offset, length)
            if hashlib.sha256(chunk).digest() != expected:
                raise IntegrityError("File content changed during the operation")
            offset += len(chunk)
            yield chunk
        if self._read(offset, 1):
            raise IntegrityError("File size changed during the operation")
        yield self.suffix


class UploadBody(httpx.SyncByteStream):
    def __init__(self, body: Multipart) -> None:
        self.body = body

    def abort(self) -> None:
        self.body.abort()

    def __iter__(self) -> Iterator[bytes]:
        body = self.body
        assert body.offset is not None
        offset = body.offset
        end = offset + body.size
        digest = hashlib.sha256()
        while offset < end:
            chunk = body._read(offset, min(_CHUNK, end - offset))
            if not chunk:
                raise IntegrityError("File size changed during the operation")
            offset += len(chunk)
            digest.update(chunk)
            if offset == end:
                if body._read(offset, 1) or digest.hexdigest() != body.sha256:
                    raise IntegrityError("File content changed during the operation")
            yield chunk

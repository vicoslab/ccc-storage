"""Newline-delimited JSON protocol for `ccc-storage` ↔ mountd."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1


class ProtocolError(ValueError):
    """Raised when protocol messages are malformed or unsupported."""


@dataclass(frozen=True)
class Request:
    command: str
    path: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "command": self.command,
            "path": self.path,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Request:
        version = int(data.get("version", 0))
        _check_version(version)
        command = str(data.get("command", ""))
        if not command:
            raise ProtocolError("missing command")
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise ProtocolError("payload must be an object")
        return cls(
            command=command,
            path=str(data.get("path", "")),
            payload=payload,
            version=version,
        )


@dataclass(frozen=True)
class Response:
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    code: str = ""
    version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "code": self.code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Response:
        version = int(data.get("version", 0))
        _check_version(version)
        result = data.get("result", {})
        if not isinstance(result, dict):
            raise ProtocolError("result must be an object")
        return cls(
            ok=bool(data.get("ok", False)),
            result=result,
            error=str(data.get("error", "")),
            code=str(data.get("code", "")),
            version=version,
        )


def _check_version(version: int) -> None:
    if version > PROTOCOL_VERSION:
        raise ProtocolError(
            f"protocol version {version} is newer than supported {PROTOCOL_VERSION}"
        )
    if version < 1:
        raise ProtocolError(f"invalid protocol version {version}")


def _loads_line(data: bytes | str) -> dict[str, Any]:
    text = data.decode() if isinstance(data, bytes) else data
    try:
        loaded = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ProtocolError(str(exc)) from exc
    if not isinstance(loaded, dict):
        raise ProtocolError("message must be a JSON object")
    return loaded


def encode_request(request: Request) -> bytes:
    return (json.dumps(request.to_dict(), sort_keys=True) + "\n").encode()


def decode_request(data: bytes | str) -> Request:
    return Request.from_dict(_loads_line(data))


def encode_response(response: Response) -> bytes:
    return (json.dumps(response.to_dict(), sort_keys=True) + "\n").encode()


def decode_response(data: bytes | str) -> Response:
    return Response.from_dict(_loads_line(data))

"""Unix-domain control socket server for mountd."""

from __future__ import annotations

import contextlib
import os
import socket
import threading
from pathlib import Path
from typing import Protocol

from ccc_layered_core.protocol import (
    ProtocolError,
    Request,
    Response,
    decode_request,
    encode_response,
)


class RequestHandler(Protocol):
    def dispatch(self, request: Request) -> Response: ...


class ControlServer:
    """Small newline-JSON Unix socket server.

    One request line produces one response line. The server is intentionally
    simple and threaded because it is control-plane only.
    """

    def __init__(
        self,
        socket_path: str | Path,
        handler: RequestHandler,
        *,
        socket_mode: int = 0o600,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.handler = handler
        self.socket_mode = socket_mode
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, self.socket_mode)
        sock.listen(20)
        sock.settimeout(0.2)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, name="ccc-layered-control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            poke = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            poke.settimeout(0.2)
            poke.connect(str(self.socket_path))
            poke.close()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                data = _read_line(conn)
                if not data:
                    return
                request = decode_request(data)
                response = self.handler.dispatch(request)
            except ProtocolError as exc:
                response = Response(ok=False, error=str(exc), code="EPROTO")
            except Exception as exc:  # keep daemon stack traces off the wire
                response = Response(ok=False, error=str(exc), code="EINTERNAL")
            with contextlib.suppress(OSError):
                conn.sendall(encode_response(response))


def _read_line(conn: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks)

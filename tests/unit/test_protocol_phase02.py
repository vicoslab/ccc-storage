from __future__ import annotations

import pytest

from ccc_layered_core.protocol import (
    ProtocolError,
    Request,
    Response,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)


def test_protocol_roundtrip_request_and_response():
    req = Request(command="status", path="dataset:foo", payload={"json": True})
    encoded_req = encode_request(req)
    assert encoded_req.endswith(b"\n")
    assert decode_request(encoded_req) == req

    res = Response(ok=True, result={"state": "clean"})
    encoded_res = encode_response(res)
    assert encoded_res.endswith(b"\n")
    assert decode_response(encoded_res) == res


def test_protocol_rejects_newer_major_version():
    with pytest.raises(ProtocolError):
        decode_request(b'{"version": 999, "command": "status"}\n')

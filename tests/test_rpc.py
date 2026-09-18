import io

import pytest

from taskspindle.rpc import MAX_MESSAGE_BYTES, RemoteError, receive, request, send


@pytest.mark.parametrize("payload", [b"", b"{}", b"[]\n", b"broken\n", b"\xff\n",
                                    b"x" * (MAX_MESSAGE_BYTES + 1)])
def test_reject_invalid_or_unbounded_messages(payload):
    with pytest.raises(RemoteError):
        receive(io.BytesIO(payload))


def test_roundtrip_and_send_bound():
    stream = io.BytesIO()
    send(stream, {"ok": True, "result": None})
    stream.seek(0)
    assert receive(stream) == {"ok": True, "result": None}
    with pytest.raises(RemoteError):
        send(stream, {"secret": "x" * MAX_MESSAGE_BYTES})


def test_unavailable_socket_does_not_expose_arguments(tmp_path):
    with pytest.raises(RemoteError) as caught:
        request(tmp_path / "absent.sock", "start", {"env": {"TOKEN": "private-secret"}})
    assert caught.value.code == "CONTROL_UNAVAILABLE"
    assert "private-secret" not in str(caught.value)

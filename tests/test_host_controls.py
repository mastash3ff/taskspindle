import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest

from taskspindle import host_controls as controls, rpc
from taskspindle.config import ConfigError


STATUS = {"action": "status", "hosts": ["wsl"]}
HOST = {"host": "wsl", "mode": "ensemble", "status": "configured", "revision": "r1", "checks": []}


def command(code):
    return (sys.executable, "-c", code)


@pytest.mark.parametrize("payload", [None, [], {}, {**STATUS, "argv": ["/bin/sh"]},
    {**STATUS, "hosts": [["wsl"]]}, {**STATUS, "hosts": ["wsl", "wsl"]},
    {**STATUS, "hosts": ["elsewhere"]}, {**STATUS, "action": "exec"},
    {**STATUS, "action": "use", "mode": "ensemble", "expected_revisions": {}},
    {**STATUS, "action": "use", "mode": "ensemble", "expected_revisions": {"wsl": "x" * 257}},
])
def test_rejects_invalid_requests(payload):
    with pytest.raises(rpc.RemoteError, match="."):
        controls.validate_request(payload)


def test_command_is_fixed_configuration():
    assert controls.configured_command({"host_controls": {"command": [sys.executable, "-m", "manager"]}}) == (sys.executable, "-m", "manager")
    for value in [[], "sh", ["python"], ["/bin/sh", "\0"]]:
        with pytest.raises(ConfigError):
            controls.configured_command({"host_controls": {"command": value}})


def test_subprocess_receives_only_validated_payload():
    code = "import json,sys; p=json.load(sys.stdin); assert p == " + repr(STATUS) + "; print(" + repr(json.dumps({"hosts": [HOST]})) + ")"
    assert controls.invoke(command(code), STATUS) == {"hosts": [HOST]}


def test_failed_host_mutation_is_preserved():
    payload = {"action": "use", "hosts": ["wsl"], "mode": "native", "expected_revisions": {"wsl": "r1"}}
    result = {"hosts": [HOST], "results": [{"host": "wsl", "ok": False, "error": "stale revision"}]}
    assert controls.invoke(command("print(" + repr(json.dumps(result)) + ")"), payload) == result


@pytest.mark.parametrize("code", ["print('x'*65537)", "import sys; sys.stderr.write('x'*65537)", "print('{}')", "import sys; print('{}'); sys.exit(1)"])
def test_bad_or_oversized_manager_output(code):
    with pytest.raises(rpc.RemoteError):
        controls.invoke(command(code), STATUS)


def test_timeout_kills_manager_process_group(tmp_path):
    marker = tmp_path / "late-write"
    code = "import time,pathlib; time.sleep(.4); pathlib.Path(" + repr(str(marker)) + ").touch()"
    with pytest.raises(rpc.RemoteError) as failure:
        controls.invoke(command(code), STATUS, timeout=.05)
    assert failure.value.code == "ADAPTER_UNAVAILABLE"
    # Child exit is awaited by the existing adapter runner; no deferred mutation survives.
    assert not marker.exists()


@pytest.fixture
def server(tmp_path):
    address = tmp_path / "ai.sock"
    manager = command("print(" + repr(json.dumps({"hosts": [HOST]})) + ")")
    with controls.ControlServer(address, manager) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            yield address
        finally:
            service.shutdown()
            thread.join(timeout=2)


def test_unix_transport_and_adapter_cli(server, tmp_path):
    assert server.stat().st_mode & 0o777 == 0o660
    assert rpc.request(server, "ai_policy", STATUS) == {"hosts": [HOST]}
    result = subprocess.run([sys.executable, "-m", "taskspindle.host_controls", "adapter", "--socket", str(server)],
        input=json.dumps(STATUS), text=True, capture_output=True,
        env={**os.environ, "TASKSPINDLE_CONFIG": str(tmp_path / "absent.toml")}, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"hosts": [HOST]}


@pytest.mark.parametrize("wire", [b"x\n", b"{\"operation\":\"exec\",\"arguments\":{}}\n", b"x" * 65537 + b"\n"])
def test_invalid_wire_messages(server, wire):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(server))
        connection.sendall(wire)
        with connection.makefile("rb") as stream:
            result = rpc.receive(stream)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_REQUEST"


def test_oversized_adapter_stdin(tmp_path):
    result = subprocess.run([sys.executable, "-m", "taskspindle.host_controls", "adapter", "--socket", str(tmp_path / "missing.sock")],
        input="x" * 65537, text=True, capture_output=True, timeout=5)
    assert result.returncode == 1
    assert "error" in json.loads(result.stdout)


def test_serve_does_not_replace_file(tmp_path):
    address = tmp_path / "ordinary-file"
    address.write_text("keep")
    with pytest.raises(ConfigError):
        controls.serve(address, command("pass"))
    assert address.read_text() == "keep"

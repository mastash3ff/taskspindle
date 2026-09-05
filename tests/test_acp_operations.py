"""Bounded session operations and partial captures over real fake-ACP stdio."""

import asyncio

import pytest

from taskspindle.acp_client import AcpError

from .test_acp_client import running_agent

MODEL_OPTIONS = [{
    "id": "model", "name": "Model", "type": "select", "currentValue": "gemini-3.7-flash-high",
    "options": [{"value": "gemini-3.8-flash-medium", "name": "Gemini"}],
}]


async def test_authentication_uses_only_explicit_advertised_method(tmp_path):
    capture = tmp_path / "auth.txt"
    async with running_agent(tmp_path, {
        "auth_methods": ["oauth-personal"], "capture_auth_to": str(capture),
    }) as worker:
        with pytest.raises(AcpError, match="does not offer"):
            await worker.authenticate("gemini-api-key")
        assert not capture.exists()
        await worker.authenticate("oauth-personal")
        assert capture.read_text() == "oauth-personal"


async def test_authentication_is_bounded_and_preserves_refusal(tmp_path):
    async with running_agent(tmp_path, {"auth_methods": ["oauth-personal"], "auth_delay": 2}) as worker:
        with pytest.raises(AcpError) as caught:
            await worker.authenticate("oauth-personal", timeout=0.05)
        assert caught.value.code == "ACP_AUTH_FAILED"
        assert caught.value.cause["exception"] == "TimeoutError"
    async with running_agent(tmp_path, {"auth_methods": ["oauth-personal"], "auth_fail": True}) as worker:
        with pytest.raises(AcpError) as caught:
            await worker.authenticate("oauth-personal")
        assert caught.value.cause["rpc_code"] == -32000


async def test_model_configuration_requires_exact_confirmation(tmp_path):
    async with running_agent(tmp_path, {"config_options": MODEL_OPTIONS}) as worker:
        session = await worker.new_session()
        await worker.set_config_option(session, "model", "gemini-3.8-flash-medium")
        assert worker.session_config_options[0]["currentValue"] == "gemini-3.8-flash-medium"
    async with running_agent(tmp_path, {
        "config_options": MODEL_OPTIONS, "unconfirmed_config": True,
    }) as worker:
        session = await worker.new_session()
        with pytest.raises(AcpError) as caught:
            await worker.set_config_option(session, "model", "gemini-3.8-flash-medium")
        assert caught.value.code == "CONFIG_UNAVAILABLE"


@pytest.mark.parametrize("operation", ["new", "load", "config"])
async def test_session_operations_cannot_hang_and_load_does_not_restart(tmp_path, operation):
    script = {"load_session": True, "config_options": MODEL_OPTIONS, f"{operation}_delay": 2}
    async with running_agent(tmp_path, script) as worker:
        worker._handshake_timeout = 0.05
        action = {"new": worker.new_session, "load": lambda: worker.load_session("original"),
                  "config": lambda: worker.set_config_option("original", "model", "requested")}[operation]
        with pytest.raises(AcpError) as caught:
            await action()
        assert caught.value.code == {
            "new": "ACP_SESSION_FAILED", "load": "RESUME_UNAVAILABLE", "config": "CONFIG_UNAVAILABLE",
        }[operation]
        assert worker._sessions == []


async def test_timeout_preserves_partial_text_and_session(tmp_path):
    async with running_agent(tmp_path, {"early_text": "partial response", "block_seconds": 3}) as worker:
        session = await worker.new_session()
        with pytest.raises(AcpError) as caught:
            await worker.prompt(session, "go", timeout=0.2)
        assert caught.value.code == "TURN_TIMEOUT"
        assert worker.last_result.text == "partial response"
        assert worker.last_result.stop_reason == "timeout"
        assert worker._sessions == [session]


async def test_client_cancellation_preserves_partial_capture(tmp_path):
    async with running_agent(tmp_path, {"early_text": "partial response", "block_seconds": 3}) as worker:
        session = await worker.new_session()
        prompt = asyncio.create_task(worker.prompt(session, "go", timeout=10))
        for _ in range(100):
            if worker._capture is not None and worker._capture.text:
                break
            await asyncio.sleep(0.01)
        prompt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await prompt
        assert worker.last_result.text == "partial response"
        assert worker.last_result.stop_reason == "cancelled"

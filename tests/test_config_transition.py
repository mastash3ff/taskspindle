"""Configuration transitions are bounded by the correlated wire response."""

from copy import deepcopy

import pytest
from acp.connection import StreamDirection, StreamEvent
from acp.schema import SetSessionConfigOptionResponse

from taskspindle.acp_client import AcpError, AcpWorker, PermissionPolicy


@pytest.mark.parametrize(
    "sequence",
    [
        "old_before",
        "new_before",
        "unrelated_response",
        "third_before",
        "model_changed",
        "mode_changed",
        "old_after",
        "refused",
    ],
)
async def test_pending_selection_allows_only_observed_old_or_requested_until_its_response(tmp_path, sequence):
    worker = AcpWorker(
        command=("unused",),
        env={},
        cwd=tmp_path,
        stderr_path=tmp_path / "stderr",
        policy=PermissionPolicy(allow_writes=False),
    )
    old = [
        {"id": "model", "name": "Model", "type": "select", "currentValue": "grok-4.6", "options": []},
        {"id": "mode", "name": "Mode", "type": "select", "currentValue": "plan", "options": []},
        {"id": "reasoning_effort", "name": "Effort", "type": "select", "currentValue": "high", "options": []},
    ]
    worker.session_config_options = deepcopy(old)
    worker._expected_config = {"model": "grok-4.6", "mode": "plan"}

    def incoming(message):
        worker._observe(StreamEvent(StreamDirection.INCOMING, message))

    def notification(options):
        incoming(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "s",
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": options,
                    },
                },
            }
        )

    class Connection:
        async def set_config_option(self, **params):
            assert params == {"session_id": "s", "config_id": "reasoning_effort", "value": "low"}
            worker._observe(
                StreamEvent(
                    StreamDirection.OUTGOING,
                    {
                        "id": 7,
                        "method": "session/set_config_option",
                        "params": {"sessionId": "s", "configId": "reasoning_effort", "value": "low"},
                    },
                )
            )
            selected = deepcopy(old)
            selected[2]["currentValue"] = "low"
            if sequence == "unrelated_response":
                incoming({"id": 6, "result": {"configOptions": old}})
            pending = deepcopy(old)
            if sequence == "new_before":
                pending = selected
            elif sequence == "third_before":
                pending[2]["currentValue"] = "xhigh"
            elif sequence == "model_changed":
                pending[0]["currentValue"] = "grok-4.5"
            elif sequence == "mode_changed":
                incoming(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": "s",
                            "update": {
                                "sessionUpdate": "current_mode_update",
                                "currentModeId": "default",
                            },
                        },
                    }
                )
            notification(pending)
            response = old if sequence == "refused" else selected
            incoming({"id": 7, "result": {"configOptions": response}})
            if sequence == "old_after":
                # This arrives before set_config_option's awaiting coroutine can resume.
                notification(old)
            return SetSessionConfigOptionResponse(config_options=response)

    worker._conn = Connection()
    if sequence in {"old_before", "new_before", "unrelated_response"}:
        await worker.set_config_option("s", "reasoning_effort", "low")
        assert worker.session_config_options[2]["currentValue"] == "low"
        assert worker._expected_config == {"model": "grok-4.6", "mode": "plan", "reasoning_effort": "low"}
        assert not worker._config_violation
    else:
        with pytest.raises(AcpError) as caught:
            await worker.set_config_option("s", "reasoning_effort", "low")
        assert caught.value.code == "CONFIG_UNAVAILABLE"
        assert worker._config_violation

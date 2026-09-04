"""Discovery: parsing the registry, finding what is installed, and proposing config blocks."""

from __future__ import annotations

import json
import platform
import stat
from pathlib import Path

import pytest

from taskspindle import cli, discover

_ARCHES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
_MACHINE = platform.machine().lower()
PLATFORM = f"{platform.system().lower()}-{_ARCHES.get(_MACHINE, _MACHINE)}"

REGISTRY = {
    "version": 1,
    "agents": [
        {
            "id": "goose",
            "name": "Goose",
            "description": "Block's agent",
            "distribution": {"binary": {PLATFORM: {"archive": "x", "cmd": "./goose", "args": ["acp"]}}},
        },
        {
            "id": "gemini",
            "name": "Gemini CLI",
            "distribution": {"npx": {"package": "@google/gemini-cli@1.0.0", "args": ["--acp"]}},
        },
        {"id": "claude-acp", "name": "Claude Code", "distribution": {"npx": {"package": "claude-acp"}}},
        {
            "id": "codex-acp",
            "name": "Codex",
            "distribution": {"npx": {"package": "@zed-industries/codex-acp@1"}},
        },
        {"id": "shim", "name": "A shim", "distribution": {"npx": {"package": "node@22"}}},
        {
            "id": "download-only",
            "name": "Elsewhere",
            "distribution": {"binary": {"windows-x86_64": {"cmd": "x.exe"}}},
        },
        "not an object",
    ],
}


def executable(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_parse_keeps_launchable_entries_and_drops_shims_and_foreign_platforms() -> None:
    agents = discover.parse_registry(json.dumps(REGISTRY).encode())

    assert [agent.id for agent in agents] == ["claude-acp", "codex-acp", "gemini", "goose"]
    by_id = {agent.id: agent for agent in agents}
    assert by_id["goose"].candidates == (("goose", ("acp",)),)
    assert by_id["gemini"].candidates == (("gemini-cli", ("--acp",)), ("gemini", ("--acp",)))
    # The adapter keeps its suffix; the bare CLI it wraps is not an ACP agent and must not match.
    assert by_id["codex-acp"].candidates == (("codex-acp", ()),)
    with pytest.raises(discover.RegistryError, match="not valid JSON"):
        discover.parse_registry(b"{")
    with pytest.raises(discover.RegistryError, match="agents"):
        discover.parse_registry(b'{"version": 1}')


def test_detect_finds_binaries_on_path_and_in_local_bin_without_running_them(tmp_path: Path) -> None:
    on_path = tmp_path / "bin"
    on_path.mkdir()
    executable(on_path, "gemini")
    local = tmp_path / "home" / ".local" / "bin"
    local.mkdir(parents=True)
    goose = executable(local, "goose")
    agents = discover.parse_registry(json.dumps(REGISTRY).encode())

    found = discover.detect(agents, env={"PATH": str(on_path), "HOME": str(tmp_path / "home")})

    assert [(item.agent.id, item.command) for item in found] == [
        ("gemini", (str(on_path / "gemini"), "--acp")),
        ("goose", (str(goose), "acp")),
    ]


def test_a_proposal_is_a_config_block_that_names_the_found_binary(tmp_path: Path) -> None:
    agents = discover.parse_registry(json.dumps(REGISTRY).encode())
    goose = next(agent for agent in agents if agent.id == "goose")
    text = discover.proposal(discover.DiscoveredAgent(agent=goose, path="/opt/goose", args=("acp",)))

    assert "[providers.goose]" in text
    assert 'command = ["/opt/goose", "acp"]' in text
    assert 'modes = ["consult", "review"]' in text
    assert "Not tested" in text
    claude = next(agent for agent in agents if agent.id == "claude-acp")
    assert discover.first_class_match(claude) == "claude"
    assert discover.profile_id(claude) == "claude-registry"


def test_fetch_prefers_the_cache_for_the_network_source_and_a_file_when_named(tmp_path: Path) -> None:
    registry_file = tmp_path / "registry.json"
    registry_file.write_text(json.dumps(REGISTRY))
    cache = tmp_path / "cache" / "acp-registry.json"

    agents, origin = discover.fetch_registry(source=str(registry_file), cache_path=cache)
    assert origin == "file"
    assert len(agents) == 4
    assert not cache.exists()  # a local file is not cached

    cache.parent.mkdir()
    cache.write_text(json.dumps({"agents": REGISTRY["agents"][:1]}))
    agents, origin = discover.fetch_registry(source="http://127.0.0.1:9/nothing", cache_path=cache)
    assert origin == "cache"
    assert [agent.id for agent in agents] == ["goose"]

    with pytest.raises(discover.RegistryError, match="could not fetch"):
        discover.fetch_registry(source="http://127.0.0.1:9/nothing", cache_path=None)


def test_the_cli_prints_proposals_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("TASKSPINDLE_CONFIG", raising=False)
    on_path = tmp_path / "bin"
    on_path.mkdir()
    executable(on_path, "goose")
    monkeypatch.setenv("PATH", str(on_path))
    registry_file = tmp_path / "registry.json"
    registry_file.write_text(json.dumps(REGISTRY))

    assert cli.main(["discover", "--registry", str(registry_file), "--all"]) == 0
    out = capsys.readouterr().out
    assert "[providers.goose]" in out
    assert "not installed: claude-acp, codex-acp, gemini" in out

    assert cli.main(["discover", "--registry", str(registry_file), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["origin"] == "file"
    assert [item["id"] for item in printed["installed"]] == ["goose"]
    assert printed["installed"][0]["command"] == [str(on_path / "goose"), "acp"]

    assert cli.main(["discover", "--registry", str(tmp_path / "missing.json")]) == 1
    assert "could not read" in capsys.readouterr().err


async def test_probe_initializes_agents_and_continues_after_timeout(tmp_path: Path) -> None:
    import sys

    root = Path(__file__).resolve().parents[1]
    stalled = tmp_path / "stall.py"
    stalled.write_text("import time; time.sleep(30)\n")
    items = [
        discover.DiscoveredAgent(
            discover.RegistryAgent("stall", "Stall", "", ()), sys.executable, (str(stalled),)),
        discover.DiscoveredAgent(
            discover.RegistryAgent("fake", "Fake", "", ()), sys.executable, ()),
    ]
    # The fake agent is installed for this test through a Python launcher in the temporary path.
    launcher = tmp_path / "fake.py"
    launcher.write_text(f"import sys; sys.path.insert(0, {str(root)!r})\n"
                        "from tests.fakes.fake_agent import main; main()\n")
    items[1] = discover.DiscoveredAgent(items[1].agent, sys.executable, (str(launcher),))
    results = await discover.probe(items, timeout=1)
    assert results["stall"]["ok"] is False
    assert results["stall"]["error"]["code"] == "ACP_HANDSHAKE_FAILED"
    assert results["stall"]["error"]["message"] == "initialize timed out after 1s"
    assert results["stall"]["agent_info"] is None
    assert results["fake"]["ok"] is True
    assert results["fake"]["agent_info"]["name"] == "taskspindle-fake-agent"
    assert results["fake"]["load_session"] is False


def test_cli_probe_is_opt_in_and_reports_failure(monkeypatch, capsys) -> None:
    agent = discover.DiscoveredAgent(discover.RegistryAgent("fake", "Fake", "", ()), "/fake", ())
    monkeypatch.setattr(discover, "fetch_registry", lambda **kw: ([agent.agent], "file"))
    monkeypatch.setattr(discover, "detect", lambda agents: [agent])
    calls = []
    async def probe(found):
        calls.append(found)
        return {"fake": {"ok": False, "error": {"code": "ACP_HANDSHAKE_FAILED", "message": "timeout"}}}
    monkeypatch.setattr(discover, "probe", probe)
    assert cli.main(["discover", "--json"]) == 0
    assert "probe" not in json.loads(capsys.readouterr().out)["installed"][0]
    assert calls == []
    assert cli.main(["discover", "--probe", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["installed"][0]["probe"]["ok"] is False
    assert len(calls) == 1

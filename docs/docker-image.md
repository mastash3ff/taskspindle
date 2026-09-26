# Container image

The image installs TaskSpindle from its frozen Python lock and its locked ACP
adapter manifests. Node, Python and uv base images are pinned by digest. Git,
bubblewrap and the three required native provider executables are available to
jobs. Provider executables come from a separate `provider-binaries` build
context containing files named `claude`, `grok` and `agy`. The context may also
contain `muse`, but it must be a resolved, version-pinned raw Muse executable,
not an auto-updating wrapper. When present, the image exposes it through the
versioned TaskSpindle runtime as a regular executable at `runtime_dir/muse`,
which preserves the no-symlink verification boundary; an absent Muse binary
does not change existing image builds. Never supply a home or authentication
directory as a build context. Record executable hashes with the deployment
image ID.

Build with:

```sh
docker build --build-arg TASKSPINDLE_HOME="$TASKSPINDLE_USER_HOME" \
  --build-context provider-binaries="$TASKSPINDLE_BINARIES" \
  --tag "$TASKSPINDLE_IMAGE" .
```

The services and jobs use UID/GID 1000. Set `TASKSPINDLE_HOME` to the host
user's existing absolute home so persisted paths remain consistent. Keep
`XDG_DATA_HOME=/opt/taskspindle/data`; this selects the adapter installed in
the image rather than a host adapter shim. OAuth directories are mounted
only at runtime, individually for the provider that needs them.

Muse remains disabled until its subscription route and worker containment are
qualified. Do not copy or mount host Muse configuration, authentication, or
session state while that gate is unresolved. Its worker profile uses a
task-specific home with XDG config, cache, data, and state directories beneath
it; Muse's default session data location is therefore contained under that
home rather than the host's `~/.local/share/muse`. Offline diagnostics may use
the staged `runtime_dir/muse` binary for commands such as `--version`, `--help`,
or schema export, but these checks do not qualify provider inference.

Worker images must be configured by their inspected immutable image ID or
repository digest. A service tag alone is insufficient for durable jobs.

Managed preparation supplies the operator's effective Git `user.name` and
`user.email` as `TASKSPINDLE_GIT_USER_NAME` and `TASKSPINDLE_GIT_USER_EMAIL` build
arguments. Only these identity fields enter the image user's Git configuration;
repository-local settings still override them. Host Git configuration, credential
helpers, hooks, and signing keys are not copied. Preparation refuses a missing
identity so acceptance cannot silently fail when committing a candidate.

AGY and Grok jobs need the packaged restricted seccomp profile at
`/opt/taskspindle/src/taskspindle/_container/agy-seccomp.json`. It permits
the namespace and mount calls required by the existing bubblewrap policy.
Workers still run as UID 1000, with all capabilities dropped and
no-new-privileges. Claude and acceptance use Docker's default
profile. See the profile's README for provenance and the exact additions.

Before activation, verify the native provider handshakes in the configured
worker environment and run `tests/test_agy_cli_policy.py` in an isolated
container with the AGY profile. These tests exercise real permitted scope
writes, denied out-of-scope and Git/control writes, read-only OAuth token
mounts, hidden inherited controls and child cancellation. An ABI/version
check alone does not establish that containment works.

The Python image uses Debian Trixie so Git supports `merge-tree --merge-base`,
which candidate acceptance requires. The build verifies this feature in a
disposable repository before installing TaskSpindle.

## Python and pytest for verification commands

A task's verification commands run in the worker container, and an
acceptance's rerun runs in the accept container. The image's `PATH` starts
with `/opt/taskspindle/.venv/bin`, TaskSpindle's own runtime venv. That venv is
installed `--no-dev` and must not gain test tools. For verification commands
only, and only inside a container (`TASKSPINDLE_WORKER_CONTAINER=1`), the
runner and the accept job move that directory to the end of `PATH`. `python3`
and `pytest` then resolve to the image's system Python, `/usr/local/bin`.
Anything that exists only in the runtime venv still resolves. The agent's own
environment and the systemd backend are unchanged.

The build installs pytest into that system Python with
`uv pip install --system --require-hashes --no-deps`. The versions and hashes
of `pytest`, `iniconfig`, `packaging`, `pluggy` and `pygments` are copied from
`uv.lock`, and `tests/test_verification_env.py` fails if the two drift apart.
The same build step checks that `python3 -m pytest --version` works and that
the runtime venv still cannot import pytest. A project whose checks need its
own dependencies, or pytest plugins such as `pytest-asyncio`, should keep
running them through its own environment, for example
`uv run --frozen pytest`. Only the image's bare `python3` gained pytest.

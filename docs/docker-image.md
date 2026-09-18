# Container image

The image installs TaskSpindle from its frozen Python lock and its locked ACP
adapter manifests. Node, Python and uv base images are pinned by digest. Git,
bubblewrap and the three native provider executables are available to jobs.
Provider executables come from a separate `provider-binaries` build context,
containing only files named `claude`, `grok` and `agy`. Resolve symlinks when
staging these executables. Never supply a home or authentication directory as
a build context. Record executable hashes with the deployment image ID.

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

Worker images must be configured by their inspected immutable image ID or
repository digest. A service tag alone is insufficient for durable jobs.

AGY jobs need the packaged restricted seccomp profile at
`/opt/taskspindle/src/taskspindle/_container/agy-seccomp.json`. It permits
the namespace and mount calls required by the existing bubblewrap policy.
Workers still run as UID 1000, with all capabilities dropped and
no-new-privileges. Other providers and acceptance use Docker's default
profile. See the profile's README for provenance and the exact additions.

Before activation, verify the native provider handshakes in the configured
worker environment and run `tests/test_agy_cli_policy.py` in an isolated
container with the AGY profile. These tests exercise real permitted scope
writes, denied out-of-scope and Git/control writes, read-only OAuth token
mounts, hidden inherited controls and child cancellation. An ABI/version
check alone does not establish that containment works.

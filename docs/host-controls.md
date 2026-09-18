# Private host AI controls

The trusted host-controls service runs only a configured fixed command. It needs
read access to the host manager and its dependencies, and write access to the
configured Codex homes. It does not need the Docker socket or provider credentials.
Keep its socket directory outside TaskSpindle's persistent task state and mount
that directory only into the dashboard and host-controls services.

```toml
[host_controls]
socket = "/run/taskspindle/ai/control.sock"
command = ["/opt/taskspindle/.venv/bin/python", "/opt/host-manager/scripts/ai_policy.py", "--host-config", "/opt/host-manager/hosts.json", "adapter"]

[ai_policy]
command = ["/opt/taskspindle/.venv/bin/python", "-m", "taskspindle.host_controls", "adapter", "--socket", "/run/taskspindle/ai/control.sock"]
hosts = ["windows", "wsl"]
```

Run `python -m taskspindle.host_controls serve` with `TASKSPINDLE_CONFIG` pointing
to this TOML, or supply an absolute `--socket` explicitly. The listener is mode
0660 and serializes manager invocations. Its lock prevents a second service from
replacing a live listener. Requests may select only `status` or `use`, the named
hosts, and (for `use`) a supported mode with exact expected host revisions. They
cannot choose executable arguments, environment variables, or filesystem paths.

Manager stdin, stdout and stderr are bounded to 64 KiB. Execution is bounded to
10 seconds, the adapter RPC to 12 seconds, fitting the dashboard's 15-second
adapter deadline. Failures return nonzero from the adapter and never imply a
successful mutation. A timeout may occur after a host transaction committed:
fetch fresh status and revisions before deciding on another request. There is no
automatic retry. Successful transport preserves each host's individual manager
result; one host's failure does not claim that another host rolled back.

No request bodies, manager stderr, or tracebacks are logged. Authentication and
revision enforcement remain in the existing dashboard and host manager.

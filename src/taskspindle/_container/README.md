The nested-sandbox worker seccomp profile derives from Moby's Apache-2.0-licensed
`seccomp/v0.2.2/seccomp/default.json`:
https://github.com/moby/profiles/blob/seccomp/v0.2.2/seccomp/default.json

It retains the default deny action and adds the calls needed by unprivileged
bubblewrap: mount, umount2, pivot_root, namespace cloning, and unshare limited
to user, mount, PID, IPC and UTS namespaces (plus FS/FILES flags).
Network and cgroup namespace cloning remain denied. The worker runs as UID
1000 with all capabilities dropped and no-new-privileges; kernel capability
checks still constrain mount operations to its nested user namespace.

This profile applies to AGY and Grok workers and their equivalent diagnostics.
It does not grant privileged mode, host namespaces, or CAP_SYS_ADMIN.

# syntax=docker/dockerfile:1.7
# Supply a build context named provider-binaries containing the required
# executables claude, grok and agy, plus the optional pinned raw Muse binary as
# muse. OAuth and session state are mounted or created only at runtime.
FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS node
FROM ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec AS uv
FROM python:3.13-slim-trixie@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
ARG TASKSPINDLE_HOME=/home/taskspindle
ARG TASKSPINDLE_GIT_USER_NAME
ARG TASKSPINDLE_GIT_USER_EMAIL
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=uv /uv /usr/local/bin/uv
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && apt-get update \
    && apt-get install -y --no-install-recommends git bubblewrap ca-certificates procps util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 taskspindle \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir "${TASKSPINDLE_HOME}" taskspindle
RUN python -c 'import subprocess,tempfile; d = tempfile.TemporaryDirectory(); subprocess.run(["git", "init", "-q", d.name], check=True); p = subprocess.run(["git", "merge-tree", "-h"], cwd=d.name, capture_output=True, text=True); assert "merge-base" in p.stdout + p.stderr, "Git must support merge-tree --merge-base"'
RUN if [ -n "$TASKSPINDLE_GIT_USER_NAME$TASKSPINDLE_GIT_USER_EMAIL" ]; then \
      test -n "$TASKSPINDLE_GIT_USER_NAME" && test -n "$TASKSPINDLE_GIT_USER_EMAIL" \
      && git config --file "${TASKSPINDLE_HOME}/.gitconfig" user.name "$TASKSPINDLE_GIT_USER_NAME" \
      && git config --file "${TASKSPINDLE_HOME}/.gitconfig" user.email "$TASKSPINDLE_GIT_USER_EMAIL"; \
    fi
RUN if [ -n "$TASKSPINDLE_GIT_USER_NAME" ]; then \
      GIT_CONFIG_NOSYSTEM=1 HOME="$TASKSPINDLE_HOME" git config --get user.name; \
    fi
WORKDIR /opt/taskspindle
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
# Verification commands run by workers resolve python3 and pytest to this system Python ahead of
# the runtime venv, which never gets test tools. Versions and hashes match uv.lock.
COPY <<EOF /tmp/verification-requirements.txt
pytest==9.1.1 --hash=sha256:37a86b45efb9a47a61a36449063e8e18d0cab3161329fc099eb21783169c4f0c
iniconfig==2.3.0 --hash=sha256:f631c04d2c48c52b84d0d0549c99ff3859c98df65b3101406327ecc7d53fbf12
packaging==26.3 --hash=sha256:d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c
pluggy==1.6.0 --hash=sha256:e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746
pygments==2.21.0 --hash=sha256:2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9
EOF
RUN uv pip install --system --python /usr/local/bin/python3 --no-cache --require-hashes --no-deps \
      -r /tmp/verification-requirements.txt \
    && rm /tmp/verification-requirements.txt \
    && /usr/local/bin/python3 -m pytest --version \
    && ! /opt/taskspindle/.venv/bin/python -c 'import pytest' 2>/dev/null
RUN --mount=type=bind,from=provider-binaries,source=/,target=/tmp/provider-binaries \
    install -m 0755 /tmp/provider-binaries/claude /usr/local/bin/claude \
    && install -m 0755 /tmp/provider-binaries/grok /usr/local/bin/grok \
    && install -m 0755 /tmp/provider-binaries/agy /usr/local/bin/agy \
    && if [ -f /tmp/provider-binaries/muse ]; then \
         install -m 0755 /tmp/provider-binaries/muse /usr/local/bin/muse; \
       fi
ENV PATH="/opt/taskspindle/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME="${TASKSPINDLE_HOME}" \
    XDG_DATA_HOME="/opt/taskspindle/data" \
    PYTHONUNBUFFERED="1" \
    LANG="C.UTF-8"
RUN mkdir -p /opt/taskspindle/data /run/taskspindle/jobs /run/taskspindle/diagnostics /run/taskspindle/ai \
    && chown -R 1000:1000 /opt/taskspindle/data /run/taskspindle "${TASKSPINDLE_HOME}"
USER 1000:1000
RUN taskspindle setup \
    && if [ -x /usr/local/bin/muse ]; then \
         runtime_dir="$(python -c 'from taskspindle.config import paths; print(paths().runtime_dir)')" \
         && install -m 0755 /usr/local/bin/muse "$runtime_dir/muse"; \
       fi
WORKDIR ${TASKSPINDLE_HOME}
CMD ["taskspindle", "web", "--host", "0.0.0.0", "--port", "8765"]

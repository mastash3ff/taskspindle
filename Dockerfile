# syntax=docker/dockerfile:1.7
# Supply a build context named provider-binaries containing ONLY the three
# executable files claude, grok and agy. OAuth state is mounted at runtime.
FROM node:24.14.0-bookworm-slim@sha256:d8e448a56fc63242f70026718378bd4b00f8c82e78d20eefb199224a4d8e33d8 AS node
FROM ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec AS uv
FROM python:3.13-slim-trixie@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
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
COPY --from=provider-binaries --chmod=0755 /claude /usr/local/bin/claude
COPY --from=provider-binaries --chmod=0755 /grok /usr/local/bin/grok
COPY --from=provider-binaries --chmod=0755 /agy /usr/local/bin/agy
ENV PATH="/opt/taskspindle/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME="${TASKSPINDLE_HOME}" \
    XDG_DATA_HOME="/opt/taskspindle/data" \
    PYTHONUNBUFFERED="1" \
    LANG="C.UTF-8"
RUN mkdir -p /opt/taskspindle/data /run/taskspindle/jobs /run/taskspindle/diagnostics /run/taskspindle/ai \
    && chown -R 1000:1000 /opt/taskspindle/data /run/taskspindle "${TASKSPINDLE_HOME}"
USER 1000:1000
RUN taskspindle setup
WORKDIR ${TASKSPINDLE_HOME}
CMD ["taskspindle", "web", "--host", "0.0.0.0", "--port", "8765"]

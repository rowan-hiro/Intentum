# syntax=docker/dockerfile:1
# The agent host as an image (MADR 0011): OpenCode at a pinned version, plus the
# backend's MCP entry point installed from the lockfile, so a measurement runs
# with nothing of the machine's: no global config, plugin, instruction file or
# session state reaches the model. Build from the repository root:
#
#   docker build -f agent_harness/hosts/opencode.Dockerfile --build-arg OPENCODE_VERSION=1.18.26 -t intentum-opencode:1.18.26 .
#
# At run time the harness mounts the run directory at /run (workspace, exports,
# opencode.json, prompt.txt) and the benchmark read-only at /data, gives the
# container an empty HOME and working directory, and passes the model key in
# the environment (see agent_harness/hosts/opencode.py).

FROM node:24-bookworm-slim
ARG OPENCODE_VERSION=1.18.26
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g opencode-ai@${OPENCODE_VERSION} && opencode --version

# The backend's MCP server, installed once from the lockfile into /opt/venv.
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /usr/local/bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_LINK_MODE=copy
WORKDIR /repo
COPY pyproject.toml uv.lock README.md ./
COPY agent_backend ./agent_backend
RUN uv python install 3.12 \
    && uv sync --frozen --no-dev \
    && /opt/venv/bin/agent-backend-mcp --help >/dev/null \
    && chmod -R a+rX /opt/venv /opt/python /repo

# Nothing of the machine reaches the model: an empty HOME and cwd, the Claude
# Code fallbacks off. The harness runs the container as the calling user so the
# files it writes under /run belong to that user.
ENV HOME=/work OPENCODE_DISABLE_CLAUDE_CODE=1
RUN mkdir -p /work /run /data && chmod 1777 /work
WORKDIR /work
ENTRYPOINT ["opencode"]

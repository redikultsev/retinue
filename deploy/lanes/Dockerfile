# A lane: the lane host (`retinue-lane`) and the pinned Claude Code CLI it runs, nothing else. One image per lane
# (ARG LANE, LANE_UID), built from the repository root (compose: context `..`).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.6.12 /uv /bin/uv

# bubblewrap and socat: the CLI's Bash sandbox, which the managed settings require (failIfUnavailable);
# CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 also refuses to start without bubblewrap.
# git: the sessions' working trees. curl: fetch-cli.sh.
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap socat git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY deploy/lanes/fetch-cli.sh deploy/lanes/cli.lock /tmp/cli/
# The CLI the lane host checks at start-up (cliengine.CLI_VERSION); its npm integrity is checked before unpacking.
RUN bash /tmp/cli/fetch-cli.sh /opt/claude && rm -rf /tmp/cli

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY retinue ./retinue
ENV UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"

# Root's and read-only to the lane: what the CLI may never be talked out of (sandbox, denied tools, hook addresses).
# The same for both lanes.
COPY --chown=root:root --chmod=0644 deploy/lanes/managed-settings.json /etc/claude-code/managed-settings.json
ARG LANE
ARG LANE_UID
RUN test -n "$LANE" && test -n "$LANE_UID" \
 && useradd --uid "$LANE_UID" --home-dir /lane/config --no-create-home lane \
 && install -d -o lane -g lane /work /lane/config /lane/state /lane/run

USER lane
CMD ["retinue-lane"]

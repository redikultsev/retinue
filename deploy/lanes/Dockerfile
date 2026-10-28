# A lane: the lane host (`retinue-lane`) and the pinned Claude Code CLI it runs, nothing else. One image per lane
# (ARG LANE, LANE_UID), built from the repository root (compose: context `..`).
FROM python:3.12-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.6.12 /uv /bin/uv

# bubblewrap and socat: the CLI's Bash sandbox, which the managed settings require (failIfUnavailable).
# git: the sessions' working trees. curl: fetch-cli.sh.
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap socat git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
# The platform's mirrors of the owner's repositories (docs/code.md) are its user's, read-only here: git trusts them as
# a clone's origin. Root's system config, so no session can widen it.
# `directory = <folder>/*` is read as a prefix only by git 2.46 and later (Debian 13 has 2.47): checked here, so an
# image with an older git fails to build instead of every clone failing at run time.
RUN printf '[safe]\n\tdirectory = /lane/mirrors/*\n' > /etc/gitconfig && chmod 0644 /etc/gitconfig \
 && git version | awk '{split($3, v, "."); exit !(v[1] > 2 || (v[1] == 2 && v[2] >= 46))}'
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
# One file per lane, baked into its image: they differ in the hosts Bash may reach and, in the room (private), in the
# web tools being the CLI's.
ARG LANE
COPY --chown=root:root --chmod=0644 deploy/lanes/managed-settings-${LANE}.json /etc/claude-code/managed-settings.json
# COPY --chmod gives the directory it creates the same 0644: without x the lane user cannot reach the file, and the
# CLI runs as if there were no managed settings at all (no sandbox, web allowed). 0755: readable, still root's.
RUN chmod 0755 /etc/claude-code
# The lane's instructions: the CLI's managed CLAUDE.md, which it reads whatever --setting-sources says; root's, like
# the settings, so no session can rewrite them. What every lane says (common.md), where files for the owner go
# (out.md), then the lane's own part if it has one (<lane>.md), and in lane-private the owner's Base (base.md,
# docs/base.md) and his repositories (code.md, docs/code.md) — docs/instructions.md.
COPY --chown=root:root --chmod=0644 deploy/lanes/instructions/ /tmp/instructions/
RUN cd /tmp/instructions && for part in common.md out.md "$LANE.md" "$([ "$LANE" = private ] && echo base.md)" \
      "$([ "$LANE" = private ] && echo code.md)"; do \
      if [ -f "$part" ]; then cat "$part"; printf '\n'; fi; done > /etc/claude-code/CLAUDE.md \
 && test -s /etc/claude-code/CLAUDE.md && chmod 0644 /etc/claude-code/CLAUDE.md \
 && cd / && rm -rf /tmp/instructions
ARG LANE_UID
RUN test -n "$LANE" && test -n "$LANE_UID" \
 && useradd --uid "$LANE_UID" --home-dir /lane/config --no-create-home lane \
 && install -d -o lane -g lane /work /lane/config /lane/state /lane/run

USER lane
CMD ["retinue-lane"]

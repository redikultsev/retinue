FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.6.12 /uv /bin/uv

# git: the router commits the assistant's turns to the knowledge base and pushes them to its hub.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 retinue
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY retinue ./retinue
# Exactly the versions in uv.lock. The Claude Agent SDK wheel carries its own `claude` CLI, so the SDK pin in
# pyproject.toml fixes the CLI version too; nothing here installs or updates a CLI separately.
ENV UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --no-dev --no-editable \
 && install -d -o retinue -g retinue /data /workspace
ENV PATH="/app/.venv/bin:$PATH"

USER retinue
# The same image runs the router (retinue-router) and every agent host (retinue-agent).
CMD ["retinue-router"]

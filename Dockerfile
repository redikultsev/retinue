FROM python:3.12-slim

RUN useradd --create-home --uid 10001 retinue
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY retinue ./retinue
RUN pip install --no-cache-dir .

USER retinue
# The same image runs the router (retinue-router) and every agent host (retinue-agent).
CMD ["retinue-router"]

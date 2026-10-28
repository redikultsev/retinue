# The face: the app (face/) built with the exact pnpm of its package.json from its lockfile, then served by the same
# pinned nginx-unprivileged as the life hub — no Node, no sources, no package manager in what runs. Build context:
# the repository root (compose); deploy/face/Dockerfile.dockerignore keeps it to face/ and this folder's nginx.conf.
FROM node:24.22.1-alpine@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53 AS build
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 CI=1
RUN corepack enable && corepack prepare pnpm@11.24.0 --activate && pnpm --version
WORKDIR /face
COPY face/package.json face/pnpm-lock.yaml face/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY face/ ./
RUN pnpm build && test -f dist/index.html && test -f dist/sw.js && test -f dist/manifest.webmanifest

FROM nginxinc/nginx-unprivileged:1.30.5-alpine-slim@sha256:3af0c10d960cc2502427fe1219c52989d309e7d65596869c60a34fd2fa2406f0
COPY deploy/face/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /face/dist /usr/share/nginx/face

# The pages' builder: deploy/pages/build.py, nothing else. It runs with no network (compose), so everything a
# published page may load (vendor.json: highlight.js, mermaid, fonts) is fetched here, at build time, each file checked
# against its sha256; the pages service serves them under /pages/_vendor/.
FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home pages
COPY build.py /app/build.py
COPY vendor.json /app/vendor.json
COPY vendor /app/vendor
RUN python3 -I /app/build.py vendor
USER 10001
CMD ["python3", "-I", "/app/build.py"]

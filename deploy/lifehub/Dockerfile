# The life hub's builder: Hugo and deploy/lifehub/build.py, nothing else. It runs with no network (compose), so
# Hugo is fetched here, at build time, and checked against the release's published sha256.
FROM python:3.12-slim
ARG HUGO_VERSION=0.167.0
ARG HUGO_SHA256=4d84519b9f619e6d4c3fb45a50157abeabeb724f859c60605f44c23def6e1169
RUN python3 -c "import hashlib, sys, urllib.request; data = urllib.request.urlopen(sys.argv[1], timeout=120).read(); \
sys.exit('hugo: sha256 does not match') if hashlib.sha256(data).hexdigest() != sys.argv[2] else open('/tmp/hugo.tgz', 'wb').write(data)" \
      "https://github.com/gohugoio/hugo/releases/download/v${HUGO_VERSION}/hugo_${HUGO_VERSION}_linux-amd64.tar.gz" \
      "${HUGO_SHA256}" \
 && tar -xzf /tmp/hugo.tgz -C /usr/local/bin hugo \
 && rm /tmp/hugo.tgz \
 && hugo version
RUN useradd --uid 10001 --no-create-home lifehub
COPY build.py /app/build.py
COPY site /app/site
USER 10001
CMD ["python3", "-I", "/app/build.py"]

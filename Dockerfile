# calsync — the poller.
#
# Stdlib-heavy by design (sqlite3, urllib), so this needs almost nothing beyond
# Python and two pure-Python wheels.
FROM python:3.12-slim

# Not root: the only writable paths are the data volume and the secrets mount,
# and neither needs privilege.
RUN useradd --create-home --uid 10001 calsync

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
# What a deployment needs besides the image. Carried inside it so a homelab can
# lay out a stack without cloning anything — `calsync init-deploy DIR` writes
# these out, and they are the versions that match the image you pulled rather
# than whatever is on a branch somewhere.
COPY docker-compose.yml .env.example ./deploy-assets/
COPY deploy/ ./deploy-assets/deploy/

RUN pip install --no-cache-dir '.[deploy]' \
 && mkdir -p /data && chown calsync:calsync /data

USER calsync
VOLUME ["/data"]
ENV CALSYNC_DB=/data/calsync.db \
    CALSYNC_SECRETS=/run/secrets/calsync/secrets.json

# `poll` honours each source's own poll_interval_s and handles SIGTERM, so
# `docker compose stop` finishes the source in flight rather than dying between
# a calendar write and its state row.
ENTRYPOINT ["sh", "-c", "exec calsync --db \"$CALSYNC_DB\" \"$@\"", "--"]
CMD ["poll"]

# flightlog server and relay in one image - the entrypoint picks the role.
# .github/workflows/container.yml publishes it as ghcr.io/chopsuei3/drone-mesh-mapper,
# which compose.yml (server) and compose.relay.yml (relay) run. To build it
# yourself instead:
#
#   docker build -t flightlog:local .        # then FLIGHTLOG_IMAGE=flightlog:local in .env
#
# Everything that changes at runtime lives in /data (bind-mount it): the
# database, the selected ports, the raw serial log, and the relay's config and
# spool. Offline tiles go in /app/tiles. See flightlog/README.md#docker.
#
# Kept to what the legacy builder understands (no BuildKit-only syntax), so
# Debian's own docker.io on a 32-bit Pi can build it too.
FROM python:3.13-slim-trixie

# tini: PID 1 that forwards SIGTERM, so `docker stop` is immediate.
# libnss-mdns: lets a relay reach the server as fl-host.local - it asks the
# host's avahi-daemon through the socket mounted at /run/avahi-daemon.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini libnss-mdns \
 && rm -rf /var/lib/apt/lists/* \
 && (grep -q mdns4_minimal /etc/nsswitch.conf \
     || sed -i 's/^hosts:.*/hosts:          files mdns4_minimal [NOTFOUND=return] dns/' /etc/nsswitch.conf)

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY static/ static/
COPY mapper_test/ mapper_test/
COPY flightlog/ flightlog/
COPY docker/entrypoint.sh /usr/local/bin/flightlog-entrypoint
# A Windows checkout can still hand over CRLF line endings; sh would choke on them.
RUN sed -i 's/\r$//' /usr/local/bin/flightlog-entrypoint \
 && chmod 755 /usr/local/bin/flightlog-entrypoint \
 && mkdir -p /data /app/tiles

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLIGHTLOG_DB=/data/flightlog.db \
    FLIGHTLOG_RELAY_CONFIG=/data/flightlog_relay.json \
    FLIGHTLOG_SPOOL=/data/flightlog_relay.db

EXPOSE 5001
ENTRYPOINT ["tini", "--", "flightlog-entrypoint"]
CMD ["server"]

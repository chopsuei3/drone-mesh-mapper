# syntax=docker/dockerfile:1
#
# flightlog server and relay in one image - the entrypoint picks the role:
#
#   docker run ... ghcr.io/chopsuei3/drone-mesh-mapper            # server on :5001
#   docker run ... ghcr.io/chopsuei3/drone-mesh-mapper relay      # a remote site's relay
#
# Everything that changes at runtime lives in /data (bind-mount it): the
# database, the selected ports, the raw serial log, and the relay's config and
# spool. Offline tiles go in /app/tiles. See docker/compose.yml and
# flightlog/README.md#docker.
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
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/flightlog-entrypoint
# A Windows checkout can still hand over CRLF line endings; sh would choke on them.
RUN sed -i 's/\r$//' /usr/local/bin/flightlog-entrypoint && mkdir -p /data /app/tiles

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLIGHTLOG_DB=/data/flightlog.db \
    FLIGHTLOG_RELAY_CONFIG=/data/flightlog_relay.json \
    FLIGHTLOG_SPOOL=/data/flightlog_relay.db

EXPOSE 5001
ENTRYPOINT ["tini", "--", "flightlog-entrypoint"]
CMD ["server"]

#!/bin/sh
# Container entrypoint: the first argument picks the role.
#
#   server [flightlog args]     the flightlog server (default), on $FLIGHTLOG_PORT
#   relay [relay args]          a remote site's relay; server/token/name from the env
#   migrate [migrate args]      import mesh-mapper.py history from /data
#   anything else               run as a command, e.g. `sh` or `python ...`
#
# Paths come from the image's environment: FLIGHTLOG_DB, FLIGHTLOG_RELAY_CONFIG
# and FLIGHTLOG_SPOOL all point into /data.
set -e

# `command: ["--retention-days", "90"]` means the server with those flags.
case "${1:-}" in
  -*|'') set -- server "$@" ;;
esac

role="$1"
shift

case "$role" in
  server)
    mkdir -p /data
    exec python -m flightlog --host 0.0.0.0 --web-port "${FLIGHTLOG_PORT:-5001}" "$@"
    ;;
  relay)
    mkdir -p /data
    exec python -m flightlog.relay "$@"
    ;;
  migrate)
    exec python -m flightlog.migrate --csv /data/cumulative_detections.csv --state-dir /data "$@"
    ;;
  *)
    exec "$role" "$@"
    ;;
esac

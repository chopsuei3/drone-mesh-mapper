#!/usr/bin/env python3
"""Install the flightlog relay on a remote site's Raspberry Pi.

The relay reads the XIAO plugged into this Pi and forwards every line to the
home flightlog server over Tailscale. Add the node on the server's Nodes page
first - it shows this exact command, with the token, once:

    python3 RPI/install_relay.py --server http://homepi:5001 --token flr_... --name north
    python3 RPI/install_relay.py ... --ports /dev/ttyACM0     # instead of auto-detect
    python3 RPI/install_relay.py --uninstall

Like install_flightlog.py it installs IN PLACE from the directory it lives in,
which needs only flightlog/ and RPI/ - no static/, no Flask. Steps:

  1. checks Tailscale is up (warns if not)
  2. checks the token against the server (GET /api/ingest/hello) - stops here
     if the server cannot be reached or refuses it
  3. installs pyserial and requests
  4. writes flightlog_relay.json (readable only by you: it holds the token)
  5. installs and starts the flightlog-relay systemd unit, granted the serial
     group, and warns about ModemManager
  6. prints `python3 -m flightlog.relay --status`
"""
import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SERVICE = 'flightlog-relay'
HERE = Path(__file__).resolve().parent.parent        # the checkout this script is in
CONFIG_NAME = 'flightlog_relay.json'
PACKAGES = ('pyserial>=3.5', 'requests>=2.25.0')

UNIT = """[Unit]
Description=flightlog relay - forwards this site's XIAO to the flightlog server
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User={user}
{groups}WorkingDirectory={install_dir}
ExecStart={python} -m flightlog.relay --config {config}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def run(cmd, **kw):
    print('  $ ' + ' '.join(cmd))
    return subprocess.run(cmd, check=False, **kw)


def have(binary):
    return shutil.which(binary) is not None


def tailscale_check():
    """Warn, don't stop: the server may be reachable some other way."""
    if not have('tailscale'):
        print('Warning: tailscale is not installed. The relay reaches the server over')
        print('         Tailscale: curl -fsSL https://tailscale.com/install.sh | sh')
        print('         then: sudo tailscale up')
        return False
    r = subprocess.run(['tailscale', 'status'], capture_output=True, text=True)
    if r.returncode != 0:
        print('Warning: Tailscale is not up here (%s).' % (r.stdout.strip() or r.stderr.strip()
                                                           or 'tailscale status failed'))
        print('         Start it with: sudo tailscale up')
        return False
    print('Tailscale is up.')
    return True


def check_token(server, token):
    """(ok, message) from /api/ingest/hello. Stdlib only - runs before pip."""
    req = urllib.request.Request(server.rstrip('/') + '/api/ingest/hello',
                                 headers={'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            j = json.loads(resp.read().decode('utf-8'))
            return True, 'the server knows this relay as "%s"' % j.get('node')
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, 'the server rejected the token (401) - copy it again from the Nodes page'
        if e.code == 403:
            return False, 'this node is disabled on the server (403) - enable it on the Nodes page'
        return False, 'the server answered HTTP %d' % e.code
    except (urllib.error.URLError, OSError) as e:
        return False, ('cannot reach %s (%s). Is Tailscale up on both machines, and is the '
                       'server running with --host 0.0.0.0?' % (server, getattr(e, 'reason', e)))


def install_deps():
    # Debian marks the system Python as externally managed (PEP 668); the flag is
    # the documented escape hatch for exactly this case.
    cmd = [sys.executable, '-m', 'pip', 'install'] + list(PACKAGES)
    if run(cmd).returncode != 0:
        run(cmd[:4] + ['--break-system-packages'] + list(PACKAGES))


def check_import(install_dir):
    """Fail now, rather than after the service is enabled and crash-looping."""
    return run([sys.executable, '-c', 'import flightlog.relay, serial, requests'],
               cwd=install_dir).returncode == 0


def write_config(path, server, token, name, ports, spool):
    cfg = {'server': server.rstrip('/'), 'token': token, 'name': name,
           'ports': 'auto' if ports == 'auto' else [p.strip() for p in ports.split(',') if p.strip()]}
    if spool:
        cfg['spool'] = spool
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump(cfg, fh, indent=2)
    try:
        os.chmod(path, 0o600)           # an existing file keeps its old mode otherwise
    except OSError:
        pass
    print('Wrote %s (mode 600: it holds the token)' % path)


def serial_group():
    """The group that owns serial devices here: dialout on Debian, uucp on Arch."""
    try:
        import grp
    except ImportError:
        return None
    for name in ('dialout', 'uucp'):
        try:
            grp.getgrnam(name)
            return name
        except KeyError:
            continue
    return None


def modemmanager_warning():
    if not have('systemctl'):
        return
    r = subprocess.run(['systemctl', 'is-active', 'ModemManager'], capture_output=True, text=True)
    if r.stdout.strip() == 'active':
        print('Warning: ModemManager is running. It probes new /dev/ttyACM devices and')
        print('         can interfere with the XIAO. If the port keeps dropping:')
        print('           sudo systemctl disable --now ModemManager')


def install_service(install_dir, config, group):
    unit = UNIT.format(user=getpass.getuser(), install_dir=install_dir, python=sys.executable,
                       config=config, groups=('SupplementaryGroups=%s\n' % group) if group else '')
    path = '/etc/systemd/system/%s.service' % SERVICE
    with tempfile.NamedTemporaryFile('w', suffix='.service', delete=False) as fh:
        fh.write(unit)
        tmp = fh.name
    try:
        if run(['sudo', 'cp', tmp, path]).returncode != 0:
            print('Could not write %s - is sudo available?' % path)
            return False
    finally:
        os.unlink(tmp)
    run(['sudo', 'systemctl', 'daemon-reload'])
    run(['sudo', 'systemctl', 'enable', SERVICE])
    run(['sudo', 'systemctl', 'restart', SERVICE])
    return True


def uninstall():
    if have('systemctl'):
        run(['sudo', 'systemctl', 'stop', SERVICE])
        run(['sudo', 'systemctl', 'disable', SERVICE])
        run(['sudo', 'rm', '-f', '/etc/systemd/system/%s.service' % SERVICE])
        run(['sudo', 'systemctl', 'daemon-reload'])
    print('Service removed. %s and the spool (flightlog_relay.db) were left in place;' % CONFIG_NAME)
    print('lines still in the spool are delivered if the relay is installed again.')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--server', help='the flightlog server, e.g. http://homepi:5001 (Tailscale name)')
    ap.add_argument('--token', help='the token shown once on the Nodes page')
    ap.add_argument('--name', help="this node's name, as on the Nodes page")
    ap.add_argument('--ports', default='auto',
                    help='"auto" finds the XIAO by its Espressif USB id; or e.g. /dev/ttyACM0')
    ap.add_argument('--spool', help='spool file (default: flightlog_relay.db beside the config)')
    ap.add_argument('--force', action='store_true',
                    help='install even if the server cannot be reached right now')
    ap.add_argument('--no-service', action='store_true', help='write the config only')
    ap.add_argument('--uninstall', action='store_true')
    args = ap.parse_args()

    if args.uninstall:
        return uninstall()
    if not args.server or not args.token:
        ap.error('--server and --token are required (copy the command from the Nodes page)')
    if not os.path.exists(os.path.join(str(HERE), 'flightlog', 'relay.py')):
        print('No flightlog/relay.py next to this script (%s).' % HERE)
        print('Copy flightlog/ and RPI/ here, keeping them side by side, and run this again.')
        return 1
    install_dir = str(HERE)
    print('Installing the relay in place from %s' % install_dir)

    tailscale_check()
    ok, msg = check_token(args.server, args.token)
    print(('Server check: ' if ok else 'Server check FAILED: ') + msg)
    if not ok and not args.force:
        print('Nothing was installed. Fix that and run this again, or pass --force to install')
        print('anyway (the relay keeps retrying, and spools everything until it gets through).')
        return 1

    install_deps()
    if not check_import(install_dir):
        print('The relay cannot be imported - see the pip output above.')
        return 1

    config = os.path.join(install_dir, CONFIG_NAME)
    write_config(config, args.server, args.token, args.name, args.ports, args.spool)

    group = serial_group()
    started = False
    if args.no_service:
        pass
    elif not sys.platform.startswith('linux') or not have('systemctl'):
        print('systemd not found - not installing a service.')
    else:
        started = install_service(install_dir, config, group)

    print('')
    if started:
        print('Service:  sudo systemctl status %s' % SERVICE)
        print('Logs:     journalctl -u %s -f' % SERVICE)
    else:
        print('Run:      cd %s && %s -m flightlog.relay --config %s'
              % (install_dir, sys.executable, config))
    print('')
    modemmanager_warning()
    print('')
    run([sys.executable, '-m', 'flightlog.relay', '--status', '--config', config], cwd=install_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())

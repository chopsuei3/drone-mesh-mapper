#!/usr/bin/env python3
"""Install flightlog as a service on a Raspberry Pi (or any systemd Linux).

flightlog is a package, not a single file, so there is nothing to wget the way
install_rpi.py fetches mesh-mapper.py. Copy the working tree to the Pi and run
this from inside it - it installs IN PLACE from the directory it lives in:

    python3 RPI/install_flightlog.py
    python3 RPI/install_flightlog.py --replace-legacy --import-legacy
    python3 RPI/install_flightlog.py --port 5000
    python3 RPI/install_flightlog.py --uninstall

`--replace-legacy` disables the mesh-mapper.py @reboot cron entry so flightlog
can own the serial port. The crontab is backed up first. `--repo <url>` clones
instead of installing in place, for when a repository does carry flightlog.

It installs a systemd unit rather than an @reboot cron job, so the service
restarts on failure, starts after the network is up, and logs to journalctl.
The unit is granted the serial group (dialout) so it can open the node's
/dev/ttyACM device without changing your account.

install_rpi.py and mesh-mapper.py are left on disk untouched.
"""
import argparse
import getpass
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SERVICE = 'flightlog'
LEGACY_TARGET = 'mesh-mapper.py'
HERE = Path(__file__).resolve().parent.parent        # the checkout this script is in
DEFAULT_CLONE_DIR = str(Path.home() / 'drone-mesh-mapper')

# What a copied tree has to contain. static/ matters as much as the package:
# app.py serves /vendor from it, so without it every page loads and the map is
# blank - a failure that gives no clue in the browser.
REQUIRED = (
    ('flightlog/__init__.py', 'flightlog/       the application package'),
    ('static/leaflet/leaflet.js', 'static/          Leaflet and fonts, served at /vendor'),
    ('requirements.txt', 'requirements.txt'),
)

UNIT = """[Unit]
Description=flightlog - drone RemoteID flight logger
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
{groups}WorkingDirectory={install_dir}
ExecStart={python} -m flightlog.app --host 0.0.0.0 --web-port {port}{extra}
Restart=on-failure
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


def has_flightlog(path):
    return os.path.exists(os.path.join(path, 'flightlog', '__init__.py'))


def missing_parts(path):
    """Descriptions of whatever REQUIRED pieces are absent from `path`."""
    return [desc for rel, desc in REQUIRED
            if not os.path.exists(os.path.join(path, *rel.split('/')))]


# Things that belong at the project root, beside the package rather than inside
# it. Anything else at the root of a flattened copy is package content.
ROOT_LEVEL = {'flightlog', 'static', 'RPI', 'mapper_test', 'requirements.txt',
              'tiles', '__pycache__', 'flightlog.db', 'flightlog_ports.json',
              'mesh-mapper.py', 'README.md'}


def looks_flattened(path):
    """True when flightlog/'s contents were copied into `path` itself.

    An easy mistake when the destination directory is also named flightlog:
    app.py and friends land at the top level, so there is no package for
    `python -m flightlog` to import.
    """
    return (not has_flightlog(path)
            and os.path.exists(os.path.join(path, 'app.py'))
            and os.path.exists(os.path.join(path, '__init__.py')))


def explain_flattened(path):
    """Print the exact move that repairs a flattened copy."""
    try:
        loose = sorted(e for e in os.listdir(path) if e not in ROOT_LEVEL)
    except OSError:
        loose = []
    print('The flightlog package looks unpacked into %s itself.' % path)
    print('app.py and __init__.py are at the top level, so there is no')
    print('flightlog/ package for python to import.')
    print('')
    print('Move them one level down:')
    print('  cd %s' % path)
    print('  mkdir -p flightlog')
    if loose:
        print('  mv %s flightlog/' % ' '.join(loose))
    print('  rm -rf __pycache__')
    print('')
    print('static/, requirements.txt, RPI/ and mapper_test/ stay where they are.')
    print('Then run this again.')
    return 1


def clone_or_update(repo, install_dir, branch):
    if os.path.isdir(os.path.join(install_dir, '.git')):
        print('Updating existing checkout at %s' % install_dir)
        run(['git', '-C', install_dir, 'fetch', '--depth', '1', 'origin', branch])
        if run(['git', '-C', install_dir, 'checkout', branch]).returncode != 0:
            return False
        run(['git', '-C', install_dir, 'reset', '--hard', 'origin/' + branch])
        return True
    print('Cloning %s (branch %s) into %s' % (repo, branch, install_dir))
    return run(['git', 'clone', '--depth', '1', '--branch', branch, repo,
                install_dir]).returncode == 0


def install_deps(install_dir):
    req = os.path.join(install_dir, 'requirements.txt')
    if not os.path.exists(req):
        return
    # Debian marks the system Python as externally managed (PEP 668); the flag is
    # the documented escape hatch for exactly this case.
    if run([sys.executable, '-m', 'pip', 'install', '-r', req]).returncode != 0:
        run([sys.executable, '-m', 'pip', 'install', '--break-system-packages', '-r', req])


def check_import(install_dir):
    """Fail now, rather than after the service is enabled and crash-looping."""
    return run([sys.executable, '-c', 'import flightlog.app, serial'],
               cwd=install_dir).returncode == 0


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


def manual_run_hint(group):
    """The service gets the group from its unit; running by hand needs membership."""
    if not group:
        return
    import grp
    user = getpass.getuser()
    g = grp.getgrnam(group)
    if user in g.gr_mem or os.getgid() == g.gr_gid:
        return
    print('Note: %s is not in the %s group. The service is granted it, but to run' % (user, group))
    print('      flightlog by hand against the node, add yourself and log back in:')
    print('        sudo usermod -aG %s %s' % (group, user))


def modemmanager_warning():
    if not have('systemctl'):
        return
    r = subprocess.run(['systemctl', 'is-active', 'ModemManager'],
                       capture_output=True, text=True)
    if r.stdout.strip() == 'active':
        print('Warning: ModemManager is running. It probes new /dev/ttyACM devices and')
        print('         can interfere with the node. If the port keeps dropping:')
        print('           sudo systemctl disable --now ModemManager')


# -- the legacy autostart --------------------------------------------------

def strip_legacy_cron(crontab_text):
    """Drop @reboot lines that launch mesh-mapper.py.

    A pure string transform so it can be tested without a crontab. Keyed on the
    script name rather than an install directory, because --install-dir may have
    been anything; install_rpi.py's own filter (:105-110) knows the directory, we
    do not. Commented-out lines are left alone.
    """
    kept, removed = [], []
    for line in (crontab_text or '').splitlines():
        s = line.strip()
        if s.startswith('@reboot') and LEGACY_TARGET in s:
            removed.append(line)
        else:
            kept.append(line)
    return '\n'.join(kept).strip(), removed


def read_crontab():
    r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else ''


def write_crontab(text):
    p = subprocess.Popen(['crontab', '-'], stdin=subprocess.PIPE, text=True)
    p.communicate((text + '\n') if text else '\n')
    return p.returncode == 0


def legacy_processes():
    """PIDs of a running mesh-mapper.py, if any."""
    if not have('pgrep'):
        return []
    r = subprocess.run(['pgrep', '-f', LEGACY_TARGET],
                       capture_output=True, text=True, check=False)
    return [p for p in r.stdout.split() if p.isdigit()]


def replace_legacy_cron():
    """Back the crontab up, then remove the legacy autostart entry."""
    if not have('crontab'):
        print('crontab not found - no legacy autostart to disable.')
        return
    current = read_crontab()
    new_text, removed = strip_legacy_cron(current)
    if not removed:
        print('No @reboot entry for %s found - nothing to disable.' % LEGACY_TARGET)
        return
    backup = os.path.expanduser('~/crontab.backup.%s' % time.strftime('%Y%m%d-%H%M%S'))
    try:
        with open(backup, 'w') as fh:
            fh.write(current)
    except OSError as e:
        print('Could not write %s (%s) - leaving the crontab alone.' % (backup, e))
        return
    print('Crontab backed up to %s' % backup)
    if write_crontab(new_text):
        for line in removed:
            print('Disabled autostart: %s' % line.strip())
    else:
        print('Could not update the crontab. Restore it with: crontab %s' % backup)


def legacy_cron_warning():
    """When --replace-legacy was not passed, say what is still autostarting."""
    if not have('crontab'):
        return
    _, found = strip_legacy_cron(read_crontab())
    if not found:
        return
    print('')
    print('Note: %s still autostarts from cron:' % LEGACY_TARGET)
    for line in found:
        print('        %s' % line.strip())
    print('      Two programs cannot hold one USB port. Re-run with --replace-legacy')
    print('      to remove that entry (the crontab is backed up first).')


def legacy_running_warning(replaced):
    """Cron is only half of it - a process started at boot still holds the port."""
    pids = legacy_processes()
    if not pids:
        return
    print('')
    print('%s is still running (pid %s) and holds the serial port.'
          % (LEGACY_TARGET, ', '.join(pids)))
    if replaced:
        print('Autostart is off now, but flightlog cannot open the node until it stops:')
    else:
        print('flightlog cannot open the node until it stops:')
    print('  kill %s        # or just reboot' % ' '.join(pids))


# -- service ---------------------------------------------------------------

def install_service(install_dir, port, retention_days, group):
    extra = (' --retention-days %d' % retention_days) if retention_days else ''
    unit = UNIT.format(user=getpass.getuser(), install_dir=install_dir,
                       python=sys.executable, port=port, extra=extra,
                       groups=('SupplementaryGroups=%s\n' % group) if group else '')
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
    print('Service removed. The files and flightlog.db were left in place.')
    print('The legacy crontab entry, if one was removed, is in ~/crontab.backup.*')
    return 0


def lan_address():
    try:
        out = subprocess.run(['hostname', '-I'], capture_output=True, text=True).stdout.split()
        return out[0] if out else None
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--replace-legacy', action='store_true',
                    help='remove the mesh-mapper.py @reboot cron entry so flightlog '
                         'can own the serial port (crontab is backed up first)')
    ap.add_argument('--repo', help='clone this repository instead of installing in place')
    ap.add_argument('--branch', default='main', help='branch to clone with --repo')
    ap.add_argument('--install-dir', default=DEFAULT_CLONE_DIR,
                    help='where --repo clones to (ignored for in-place installs)')
    ap.add_argument('--port', type=int, default=5001)
    ap.add_argument('--retention-days', type=int, default=0)
    ap.add_argument('--import-legacy', action='store_true',
                    help='import cumulative_detections.csv after install')
    ap.add_argument('--no-service', action='store_true')
    ap.add_argument('--uninstall', action='store_true')
    args = ap.parse_args()

    if args.uninstall:
        return uninstall()

    if not sys.platform.startswith('linux'):
        print('This installer sets up a systemd service, which needs Linux.')
        print('On this machine, run flightlog directly from the checkout:')
        print('  python -m flightlog')
        return 0

    if args.repo:
        if not have('git'):
            print('git is required for --repo. Install it with: sudo apt install -y git')
            return 1
        install_dir = os.path.abspath(os.path.expanduser(args.install_dir))
        os.makedirs(os.path.dirname(install_dir), exist_ok=True)
        if not clone_or_update(args.repo, install_dir, args.branch):
            print('Could not fetch %s.' % args.repo)
            return 1
    elif has_flightlog(str(HERE)):
        install_dir = str(HERE)
        print('Installing in place from %s' % install_dir)
    elif looks_flattened(str(HERE)):
        return explain_flattened(str(HERE))
    else:
        print('No flightlog/ package next to this script (%s).' % HERE)
        print('Copy the working tree across and run this from inside it, or pass')
        print('--repo with a repository that carries flightlog.')
        return 1

    gaps = missing_parts(install_dir)
    if gaps:
        print('')
        print('%s is missing pieces flightlog needs:' % install_dir)
        for g in gaps:
            print('  - %s' % g)
        print('')
        print('Copy the whole set over, keeping the layout - RPI/ and flightlog/')
        print('must stay siblings:')
        print('  flightlog/  static/  requirements.txt  RPI/install_flightlog.py')
        return 1

    install_deps(install_dir)
    if not check_import(install_dir):
        print('flightlog cannot be imported - see the pip output above.')
        return 1

    if args.import_legacy:
        if os.path.exists(os.path.join(install_dir, 'cumulative_detections.csv')):
            print('Importing legacy history...')
            run([sys.executable, '-m', 'flightlog.migrate'], cwd=install_dir)
        else:
            print('No cumulative_detections.csv found - skipping import.')

    if args.replace_legacy:
        replace_legacy_cron()

    group = serial_group()
    ok = False
    if args.no_service:
        pass
    elif not have('systemctl'):
        print('systemd not found - skipping the service.')
    else:
        ok = install_service(install_dir, args.port, args.retention_days, group)

    ports = sorted(glob.glob('/dev/ttyACM*') + glob.glob('/dev/ttyUSB*'))
    addr = lan_address() or '<pi-address>'
    print('')
    print('Installed from %s' % install_dir)
    if ok:
        print('Service:  sudo systemctl status %s' % SERVICE)
        print('Logs:     journalctl -u %s -f' % SERVICE)
    else:
        print('Run:      cd %s && %s -m flightlog.app --host 0.0.0.0 --web-port %d'
              % (install_dir, sys.executable, args.port))
    print('Open:     http://%s:%d/sources   (tick the node\'s port there)' % (addr, args.port))
    print('Serial:   %s' % (', '.join(ports) if ports
                            else 'no /dev/ttyACM* or /dev/ttyUSB* yet - plug the node in'))
    print('')
    manual_run_hint(group)
    modemmanager_warning()
    if not args.replace_legacy:
        legacy_cron_warning()
    legacy_running_warning(args.replace_legacy)
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Start a static HTTPS probe with an automatic two-hour rollback."""
import json
import os
import pwd
import socket
import subprocess
from datetime import datetime, timedelta, timezone

from cleanup import BINARY, ROOT, cleanup, command, containers, rule, ufw_hashes

DOMAIN = 'beresta-netcheck-20261030.185-106-94-251.sslip.io'
LIFETIME = 7200


def main():
    if os.geteuid() != 0 or ROOT.is_symlink() or ROOT.resolve() != ROOT:
        raise SystemExit('Use root and the fixed temporary runtime directory')
    if (ROOT / 'before.json').exists():
        raise SystemExit('A probe already exists; stop it before starting another')
    if '185.106.94.251' not in command('ip', '-4', 'address', 'show'):
        raise SystemExit('This probe is intended only for the Helsinki VPS')
    for port in (80, 443):
        if command('ss', '-H', '-ltn', f'sport = :{port}'):
            raise SystemExit(f'Port {port} already has a listener')
        with socket.socket() as stream:
            stream.bind(('0.0.0.0', port))
    subprocess.run([str(BINARY), 'validate', '--config', str(ROOT / 'Caddyfile'),
                    '--adapter', 'caddyfile'], check=True, env={
                        **os.environ, 'XDG_DATA_HOME': str(ROOT / 'state'),
                        'XDG_CONFIG_HOME': str(ROOT / 'state'),
                    })
    before = {'iptables': command('iptables', '-S'), 'ufw': ufw_hashes(),
              'containers': containers()}
    (ROOT / 'before.json').write_text(json.dumps(before, indent=2))
    (ROOT / 'before.json').chmod(0o600)
    state = ROOT / 'state'
    state.mkdir(exist_ok=True, mode=0o700)
    nobody = pwd.getpwnam('nobody')
    # validate may have created empty state directories; keep their owner consistent.
    for path in [state, *state.rglob('*')]:
        if path.is_symlink():
            raise SystemExit('Unsafe state directory')
        os.chown(path, nobody.pw_uid, nobody.pw_gid)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=LIFETIME)
    expiry_msk = expiry.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M')
    index = ROOT / 'www' / 'index.html'
    index.write_text(index.read_text().replace('__EXPIRES_MSK__', expiry_msk))
    try:
        # Independent root timer also closes the probe if the main service fails.
        subprocess.run([
            'systemd-run', '--unit=beresta-vpn-netcheck-expiry', '--collect',
            f'--on-active={LIFETIME}s', '--timer-property=AccuracySec=1s',
            '/usr/bin/python3', str(ROOT / 'cleanup.py'), '--stop',
        ], check=True)
        for port in (80, 443):
            subprocess.run(['iptables', '-w', '5', '-I', 'INPUT', '1', *rule(port)[1:]], check=True)
        subprocess.run([
            'systemd-run', '--unit=beresta-vpn-netcheck', '--collect',
            '--property=User=nobody', '--property=Group=nogroup',
            f'--property=RuntimeMaxSec={LIFETIME}', '--property=TimeoutStopSec=15s',
            '--property=Restart=no', '--property=MemoryMax=128M',
            '--property=CPUQuota=25%', '--property=TasksMax=64', '--property=LimitNOFILE=512',
            '--property=CapabilityBoundingSet=CAP_NET_BIND_SERVICE',
            '--property=AmbientCapabilities=CAP_NET_BIND_SERVICE',
            '--property=NoNewPrivileges=yes', '--property=ProtectSystem=full',
            '--property=ProtectHome=yes', '--property=PrivateTmp=yes',
            '--property=PrivateDevices=yes', '--property=ProtectKernelTunables=yes',
            '--property=ProtectKernelModules=yes', '--property=ProtectControlGroups=yes',
            '--property=RestrictSUIDSGID=yes', '--property=LockPersonality=yes',
            '--property=UMask=0077',
            f'--property=ExecStopPost=+/usr/bin/python3 {ROOT}/cleanup.py',
            f'--setenv=XDG_DATA_HOME={state}', f'--setenv=XDG_CONFIG_HOME={state}',
            '--setenv=GOMEMLIMIT=80MiB', '--setenv=GOMAXPROCS=1',
            str(BINARY), 'run', '--config', str(ROOT / 'Caddyfile'),
            '--adapter', 'caddyfile',
        ], check=True)
    except BaseException:
        subprocess.run(['systemctl', 'stop', 'beresta-vpn-netcheck.service'], check=False)
        cleanup()
        raise
    print(json.dumps({'url': 'https://' + DOMAIN, 'expires_utc': expiry.isoformat(),
                      'expires_moscow': expiry_msk}, ensure_ascii=False))


if __name__ == '__main__':
    main()

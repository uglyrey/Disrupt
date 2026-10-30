"""Remove only this temporary probe and its two tagged runtime firewall rules."""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path('/run/beresta-vpn-netcheck')
BINARY = Path('/opt/beresta-netcheck-caddy')
AUDIT = Path('/run/beresta-vpn-netcheck-cleanup.json')
TAG = 'beresta-vpn-netcheck-20261030'


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def ufw_hashes():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path('/etc/ufw').glob('user*.rules'))}


def containers():
    names = ['beresta-bot-telegram-1', 'beresta-bot-ai-relay-1']
    return {
        name: command('docker', 'inspect', '--format', '{{.Id}} {{.State.StartedAt}}', name)
        for name in names
    }


def rule(port):
    return ['INPUT', '-p', 'tcp', '--dport', str(port), '-m', 'comment',
            '--comment', TAG, '-j', 'ACCEPT']


def closed(port):
    with socket.socket() as stream:
        stream.settimeout(1)
        return stream.connect_ex(('127.0.0.1', port)) != 0


def cleanup():
    if os.geteuid() != 0:
        raise SystemExit('Root required for restoring the temporary firewall rules')
    if not ROOT.exists():
        return
    if ROOT.is_symlink() or ROOT.resolve() != ROOT:
        raise SystemExit('Unsafe runtime directory')
    before = json.loads((ROOT / 'before.json').read_text())
    for port in (80, 443):
        for _ in range(16):
            if subprocess.run(['iptables', '-w', '5', '-C', *rule(port)],
                              capture_output=True).returncode:
                break
            subprocess.run(['iptables', '-w', '5', '-D', *rule(port)], check=True)
    checks = {
        'iptables_restored': command('iptables', '-S') == before['iptables'],
        'ufw_unchanged': ufw_hashes() == before['ufw'],
        'bot_containers_not_restarted': containers() == before['containers'],
        'temporary_ports_closed': all(closed(port) for port in (80, 443)),
    }
    AUDIT.write_text(json.dumps(checks, indent=2) + '\n')
    AUDIT.chmod(0o600)
    if BINARY.exists():
        if BINARY.is_symlink() or BINARY.resolve() != BINARY:
            raise SystemExit('Unsafe probe executable path')
        BINARY.unlink()
    subprocess.run(['systemctl', 'stop', 'beresta-vpn-netcheck-expiry.timer'],
                   capture_output=True, check=False)
    # The directory is fixed, checked above, and contains only this probe's files.
    shutil.rmtree(ROOT)
    print(json.dumps(checks))


if __name__ == '__main__':
    if '--stop' in sys.argv:
        subprocess.run(['systemctl', 'stop', 'beresta-vpn-netcheck.service'], check=True)
    cleanup()

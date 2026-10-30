"""A temporary static hostname on the existing Russian Caddy, with scoped rollback."""
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path('/run/beresta-vpn-russia-check')
AUDIT = Path('/run/beresta-vpn-russia-check-result.json')
DOMAIN = 'beresta-netcheck-20261030.178-217-98-101.sslip.io'
ROUTE_ID = 'beresta-vpn-russia-check-20261030'
CONTAINER = 'beresta-pilot-caddy-1'
LIFETIME = 3600


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def request(method, path, value=None, etag=None):
    client = http.client.HTTPConnection('127.0.0.1', 2019, timeout=15)
    headers = {}
    body = None
    if value is not None:
        body = json.dumps(value, ensure_ascii=False).encode()
        headers['Content-Type'] = 'application/json'
    if etag:
        headers['If-Match'] = etag
    client.request(method, path, body, headers)
    response = client.getresponse()
    data = response.read()
    status, result_etag = response.status, response.getheader('Etag')
    client.close()
    if status not in (200, 404):
        raise RuntimeError(f'Caddy API {method} {path}: HTTP {status}')
    return status, json.loads(data) if data and status == 200 else None, result_etag


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot():
    names = ['beresta-pilot-' + name + '-1' for name in ('caddy', 'api', 'worker', 'scheduler', 'db')]
    return {
        'containers': {name: command('docker', 'inspect', '--format',
                                    '{{.Id}} {{.State.StartedAt}}', name) for name in names},
        'iptables': command('nsenter', '--target', '1', '--net', 'iptables', '-S'),
        'ufw': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path('/etc/ufw').glob('user*.rules'))},
        'caddyfile_hash': command('sha256sum', '/opt/beresta/current/deploy/Caddyfile').split()[0],
    }


def route_html(expiry):
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>beresta · контрольный адрес в России</title>
<body><main><h1>Контрольная страница открылась</h1>
<p>Это тот же российский сервер, где работает Beresta. Его IP не менялся.
Для этой страницы используются другое доменное имя и другой DNS-сервис.</p>
<p>Здесь нет входа, заметок, аналитики и автоматических запросов к приложению.</p>
<p>Оставьте VPN включённым. В тот же момент откройте
<a href="https://app.berestaapp.ru/" target="_blank" rel="noopener noreferrer">основной сайт</a>
и сообщите Марку, открылись ли оба адреса. При ошибке перепишите её код из браузера.</p>
<p>Тестовый адрес работает до {expiry} по Москве.</p>
<p>Не вводите на контрольной странице пароль или код входа.</p></main></body></html>'''


def make_route(expiry):
    headers = {
        'Cache-Control': ['no-store'], 'Referrer-Policy': ['no-referrer'],
        'X-Content-Type-Options': ['nosniff'],
        'Content-Security-Policy': ["default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"],
    }

    def response(body, code, mime):
        return {'handler': 'static_response', 'status_code': code, 'body': body,
                'headers': {**headers, 'Content-Type': [mime]}}

    return {
        '@id': ROUTE_ID, 'match': [{'host': [DOMAIN]}], 'terminal': True,
        'handle': [{'handler': 'subroute', 'routes': [
            {'match': [{'path': ['/'], 'method': ['GET', 'HEAD']}],
             'handle': [response(route_html(expiry), 200, 'text/html; charset=utf-8')]},
            {'match': [{'path': ['/health'], 'method': ['GET', 'HEAD']}],
             'handle': [response('{"status":"ok","purpose":"russian-network-diagnostic"}',
                                 200, 'application/json')]},
            {'handle': [response('Not found', 404, 'text/plain; charset=utf-8')]},
        ]}],
    }


def start():
    if '178.217.98.101' not in command('nsenter', '--target', '1', '--net', 'ip', '-4', 'address', 'show'):
        raise SystemExit('This check is intended only for the Russian VPS')
    if (ROOT / 'before.json').exists():
        raise SystemExit('A Russian probe already exists')
    status, cfg, etag = request('GET', '/config/')
    if status != 200:
        raise SystemExit('Caddy config unavailable')
    if request('GET', '/id/' + ROUTE_ID)[0] != 404:
        raise SystemExit('Temporary route ID already exists')
    servers = cfg['apps']['http']['servers']
    candidates = [name for name, server in servers.items()
                  if ':443' in server.get('listen', []) and server.get('routes')]
    if len(candidates) != 1:
        raise SystemExit('Expected exactly one existing HTTPS server')
    before = {**snapshot(), 'config_hash': digest(cfg)}
    (ROOT / 'before.json').write_text(json.dumps(before, indent=2))
    (ROOT / 'before.json').chmod(0o600)
    expiry = datetime.now(timezone.utc) + timedelta(seconds=LIFETIME)
    expiry_msk = expiry.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M')
    subprocess.run([
        'systemd-run', '--unit=beresta-vpn-russia-check-expiry', '--collect',
        f'--on-active={LIFETIME}s', '--timer-property=AccuracySec=1s',
        '/usr/bin/python3', str(ROOT / 'russia_check.py'), 'rollback',
    ], check=True)
    try:
        # PUT inserts into this array. If-Match prevents overwriting concurrent work.
        request('PUT', f'/config/apps/http/servers/{candidates[0]}/routes/0',
                make_route(expiry_msk), etag)
        status, route, _ = request('GET', '/id/' + ROUTE_ID)
        if status != 200 or route.get('match') != [{'host': [DOMAIN]}]:
            raise RuntimeError('Temporary hostname verification failed')
    except BaseException:
        rollback()
        raise
    print(json.dumps({'url': 'https://' + DOMAIN, 'expires_moscow': expiry_msk,
                      'containers_not_restarted': snapshot()['containers'] == before['containers']},
                     ensure_ascii=False))


def remove_test_certificates():
    mounts = json.loads(command('docker', 'inspect', '--format', '{{json .Mounts}}', CONTAINER))
    matches = [Path(mount['Source']) for mount in mounts if mount['Destination'] == '/data']
    if len(matches) != 1:
        raise RuntimeError('Expected one Caddy data mount')
    base = matches[0] / 'caddy' / 'certificates'
    removed = 0
    if not base.exists():
        return removed
    for issuer in base.iterdir():
        target = issuer / DOMAIN
        if target.exists():
            if (target.is_symlink() or not target.is_dir()
                    or target.resolve() != base.resolve() / issuer.name / DOMAIN):
                raise RuntimeError('Unsafe test-certificate directory')
            expected_names = {DOMAIN + suffix for suffix in ('.crt', '.key', '.json')}
            if any(p.name not in expected_names or p.is_symlink() for p in target.iterdir()):
                raise RuntimeError('Unexpected file in test-certificate directory')
            # Only this temporary hostname's certificates, never the ACME account or app certificate.
            shutil.rmtree(target)
            removed += 1
    return removed


def rollback():
    if not (ROOT / 'before.json').exists():
        raise RuntimeError('Missing original configuration fingerprint')
    before = json.loads((ROOT / 'before.json').read_text())
    status, route, etag = request('GET', '/id/' + ROUTE_ID)
    if status == 200:
        if route.get('match') != [{'host': [DOMAIN]}]:
            raise RuntimeError('Refusing to remove an unexpected route')
        request('DELETE', '/id/' + ROUTE_ID, etag=etag)
    _, after_config, _ = request('GET', '/config/')
    after = snapshot()
    checks = {
        'temporary_route_removed': request('GET', '/id/' + ROUTE_ID)[0] == 404,
        'original_config_restored': digest(after_config) == before['config_hash'],
        'containers_not_restarted': after['containers'] == before['containers'],
        'iptables_unchanged': after['iptables'] == before['iptables'],
        'ufw_unchanged': after['ufw'] == before['ufw'],
        'production_caddyfile_unchanged': after['caddyfile_hash'] == before['caddyfile_hash'],
        'test_certificate_directories_removed': remove_test_certificates(),
    }
    AUDIT.write_text(json.dumps(checks, indent=2) + '\n')
    AUDIT.chmod(0o600)
    print(json.dumps(checks))
    subprocess.run(['systemctl', 'stop', 'beresta-vpn-russia-check-expiry.timer'],
                   capture_output=True, check=False)
    if ROOT.is_symlink() or ROOT.resolve() != ROOT:
        raise RuntimeError('Unsafe temporary directory')
    shutil.rmtree(ROOT)


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('Root required')
    if ROOT.is_symlink() or ROOT.resolve() != ROOT or not Path(__file__).resolve().is_relative_to(ROOT):
        raise SystemExit('Use the fixed temporary runtime directory')
    if '--in-caddy-net' not in sys.argv:
        pid = command('docker', 'inspect', '--format', '{{.State.Pid}}', CONTAINER)
        subprocess.run(['nsenter', '--target', pid, '--net', '/usr/bin/python3',
                        str(Path(__file__).resolve()), *sys.argv[1:], '--in-caddy-net'], check=True)
    elif sys.argv[1] == 'start':
        start()
    elif sys.argv[1] == 'rollback':
        rollback()
    else:
        raise SystemExit('Use start or rollback')

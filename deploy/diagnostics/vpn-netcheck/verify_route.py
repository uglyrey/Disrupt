"""Verify scoped Caddy API edits inside a disposable network-isolated Caddy."""
import http.client
import json
import subprocess
import sys
import time
from pathlib import Path

import russia_check as probe

TEST_CONTAINER = 'beresta-vpn-route-check'
TEST_CONFIG = Path('/run/beresta-vpn-russia-check/isolated-caddy.json')


def inside():
    for attempt in range(50):
        try:
            _, original, etag = probe.request('GET', '/config/')
            break
        except ConnectionRefusedError:
            if attempt == 49:
                raise
            time.sleep(0.2)
    before = probe.digest(original)
    probe.request('PUT', '/config/apps/http/servers/srv0/routes/0',
                  probe.make_route('test only'), etag)

    def get(host, path):
        client = http.client.HTTPConnection('127.0.0.1', 443, timeout=3)
        client.request('GET', path, headers={'Host': host})
        response = client.getresponse()
        result = response.status, response.read().decode()
        client.close()
        return result

    assert get('app.berestaapp.ru', '/') == (200, 'original route')
    status, body = get(probe.DOMAIN, '/')
    assert status == 200 and 'beresta' in body
    for path in ('/admin', '/admin-api/v1/me', '/internal', '/api/v1/notes', '/review'):
        assert get(probe.DOMAIN, path)[0] == 404, path
    _, route, etag = probe.request('GET', '/id/' + probe.ROUTE_ID)
    assert route['match'] == [{'host': [probe.DOMAIN]}]
    probe.request('DELETE', '/id/' + probe.ROUTE_ID, etag=etag)
    _, restored, _ = probe.request('GET', '/config/')
    assert probe.digest(restored) == before
    assert get('app.berestaapp.ru', '/') == (200, 'original route')
    assert probe.request('GET', '/id/' + probe.ROUTE_ID)[0] == 404
    print('Isolated route insertion, original hostname, private-route denial and exact rollback passed')


def outside():
    TEST_CONFIG.write_text(json.dumps({
        'apps': {'http': {'servers': {'srv0': {
            'listen': [':443'], 'automatic_https': {'disable': True},
            'routes': [{'match': [{'host': ['app.berestaapp.ru']}],
                        'handle': [{'handler': 'static_response', 'body': 'original route'}]}],
        }}}},
    }))
    image = probe.command('docker', 'inspect', '--format', '{{.Image}}', probe.CONTAINER)
    subprocess.run([
        'docker', 'run', '--detach', '--rm', '--name', TEST_CONTAINER, '--network', 'none',
        '--memory', '64m', '--cpus', '0.25', '--pids-limit', '64', '--read-only',
        '--tmpfs', '/tmp:size=8m', '--tmpfs', '/data:size=8m', '--tmpfs', '/config:size=8m',
        '--mount', f'type=bind,source={TEST_CONFIG},target=/etc/caddy/test.json,readonly',
        image, 'caddy', 'run', '--config', '/etc/caddy/test.json',
    ], check=True, stdout=subprocess.DEVNULL)
    try:
        pid = probe.command('docker', 'inspect', '--format', '{{.State.Pid}}', TEST_CONTAINER)
        subprocess.run(['nsenter', '--target', pid, '--net', 'python3',
                        str(Path(__file__).resolve()), '--inside'], check=True)
    finally:
        subprocess.run(['docker', 'stop', '-t', '2', TEST_CONTAINER], check=True,
                       stdout=subprocess.DEVNULL)
        TEST_CONFIG.unlink(missing_ok=True)


if __name__ == '__main__':
    if '--inside' in sys.argv:
        inside()
    else:
        outside()

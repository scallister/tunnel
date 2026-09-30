import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'tunnel'


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('TUNNEL_')}
        self.env.update(XDG_DATA_HOME=str(self.root), TUNNEL_DOMAIN='example.com',
                        TUNNEL_SERVER='control.example.com', TUNNEL_TOKEN='test-only')
        arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
        client = self.root / 'private-tunnel' / f'frpc-0.71.0-{arch}'
        client.parent.mkdir()
        client.write_text('''#!/usr/bin/env python3
import json, os, pathlib, stat, sys, tomllib
p = pathlib.Path(sys.argv[2])
assert stat.S_IMODE(p.stat().st_mode) == 0o600
config = tomllib.loads(p.read_text())
pathlib.Path(os.environ['XDG_DATA_HOME'], 'capture.json').write_text(json.dumps(config))
pathlib.Path(os.environ['XDG_DATA_HOME'], 'config-path').write_text(str(p))
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
''')
        client.chmod(0o755)

    def run_client(self, *args):
        return subprocess.run([str(SCRIPT), *args], env=self.env,
                              text=True, capture_output=True)

    def test_help_without_configuration(self):
        for key in ('TUNNEL_DOMAIN', 'TUNNEL_SERVER', 'TUNNEL_TOKEN'):
            del self.env[key]
        result = self.run_client('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('example.com', result.stdout)

    def test_each_required_setting(self):
        for key in ('TUNNEL_DOMAIN', 'TUNNEL_SERVER', 'TUNNEL_TOKEN'):
            with self.subTest(key=key):
                value = self.env.pop(key)
                result = self.run_client('http', '8080')
                self.assertEqual(result.returncode, 2)
                self.assertIn(key, result.stderr)
                self.env[key] = value

    def test_configuration_escaping_and_cleanup(self):
        self.env['TUNNEL_TOKEN'] = 'test-"quoted"-\\slash'
        result = self.run_client('http', 'localhost:08080', 'MyApp')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads((self.root / 'capture.json').read_text())
        self.assertEqual(config['auth']['token'], self.env['TUNNEL_TOKEN'])
        self.assertEqual(config['serverAddr'], 'control.example.com')
        self.assertEqual(config['proxies'][0]['localPort'], 8080)
        self.assertEqual(config['proxies'][0]['subdomain'], 't-myapp')
        self.assertIn('https://t-myapp.example.com', result.stdout)
        self.assertNotIn(self.env['TUNNEL_TOKEN'], result.stdout + result.stderr)
        self.assertFalse(Path((self.root / 'config-path').read_text()).exists())

    def test_failure_status_and_cleanup(self):
        self.env['FAKE_EXIT'] = '7'
        result = self.run_client('http', '8080')
        self.assertEqual(result.returncode, 7)
        self.assertFalse(Path((self.root / 'config-path').read_text()).exists())

    def test_rejects_bad_targets(self):
        for target in ('https://localhost:8080', 'host:extra:80', '65536', '0', '[::1]:80'):
            with self.subTest(target=target):
                self.assertEqual(self.run_client('http', target).returncode, 2)

    def test_rejects_config_injection(self):
        for key, value in [('TUNNEL_SERVER', 'host"\ninjected=true'),
                           ('TUNNEL_DOMAIN', 'https://example.com'),
                           ('TUNNEL_SERVER_PORT', '0'),
                           ('TUNNEL_PROTOCOL', 'invalid'),
                           ('TUNNEL_TOKEN', 'test\nnewline')]:
            with self.subTest(key=key):
                old = self.env[key] if key in self.env else None
                self.env[key] = value
                self.assertEqual(self.run_client('http', '8080').returncode, 2)
                if old is None:
                    del self.env[key]
                else:
                    self.env[key] = old

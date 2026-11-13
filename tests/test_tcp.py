import asyncio
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import pwd
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tunnel_tcp
from websockets.asyncio.server import serve
from websockets.sync.client import connect


def wait_for(check, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.03)
    raise AssertionError('timed out waiting for test fixture')


class TCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('TUNNEL_')}
        self.env.update(XDG_DATA_HOME=str(self.root), TUNNEL_DOMAIN='example.com',
                        TUNNEL_SERVER='control.example.com', TUNNEL_TOKEN='test-only',
                        TUNNEL_PYTHON=sys.executable, NO_PROXY='localhost,127.0.0.1',
                        no_proxy='localhost,127.0.0.1')
        arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]
        client = self.root / 'private-tunnel' / f'frpc-0.71.0-{arch}'
        client.parent.mkdir()
        client.write_text('''#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time, tomllib
p = pathlib.Path(sys.argv[2])
config = tomllib.loads(p.read_text())
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
pathlib.Path(os.environ['XDG_DATA_HOME'], 'capture.json').write_text(json.dumps({'port':config['proxies'][0]['localPort'], 'config':str(p), 'child':child.pid}))
if os.environ.get('FAKE_FAIL'):
    sys.exit(7)
time.sleep(60)
''')
        client.chmod(0o755)

    def test_duration_bounds_and_secure_urls(self):
        self.assertEqual(tunnel_tcp.duration('30m'), 1800)
        self.assertEqual(tunnel_tcp.duration('4h'), 14400)
        for value in ('0', '0m', '-1', '5h', 'forever', '1.5h'):
            with self.assertRaises(Exception):
                tunnel_tcp.duration(value)
        for url in ('http://localhost', 'https://u:p@host', 'https://host/path', 'https://host?q=x'):
            with self.assertRaises(ValueError):
                tunnel_tcp.websocket_url(url)
        self.assertEqual(tunnel_tcp.websocket_url('example.com'), 'wss://example.com/')

    def start_tunnel(self, timeout='2s', port=22):
        process = subprocess.Popen([str(ROOT/'tunnel'), 'tcp', str(port), '--timeout', timeout],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        capture = self.root/'capture.json'
        wait_for(capture.exists)
        return process, json.loads(capture.read_text())

    def assert_gone(self, pid):
        def gone():
            try:
                # Zombies have no running code or open sockets; this container's
                # PID 1 may reap them later than a normal system init would.
                return Path(f'/proc/{pid}/stat').read_text().split()[2] == 'Z'
            except FileNotFoundError:
                return True
        wait_for(gone)

    def test_expiry_closes_listener_and_kills_tunnel_children(self):
        process, capture = self.start_tunnel()
        output, error = process.communicate(timeout=7)
        self.assertEqual(process.returncode, 0, error)
        self.assertIn('expired', output)
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', capture['port']), timeout=1)
        self.assertFalse(Path(capture['config']).exists())
        self.assert_gone(capture['child'])

    def test_signal_cleanup(self):
        process, capture = self.start_tunnel('30s')
        process.send_signal(signal.SIGTERM)
        _, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error)
        self.assertFalse(Path(capture['config']).exists())
        self.assert_gone(capture['child'])

    def test_expiry_disconnects_an_active_session(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0)); listener.listen()
        self.addCleanup(listener.close)
        disconnected = threading.Event()
        def echo():
            connection, _ = listener.accept()
            with connection:
                while data := connection.recv(65536):
                    connection.sendall(data)
            disconnected.set()
        thread = threading.Thread(target=echo, daemon=True); thread.start()
        process, capture = self.start_tunnel('2s', listener.getsockname()[1])
        with connect(f"ws://127.0.0.1:{capture['port']}", proxy=None) as websocket:
            websocket.send(b'active TCP session')
            self.assertEqual(websocket.recv(), b'active TCP session')
            with self.assertRaises(Exception):
                websocket.recv(timeout=5)
        process.communicate(timeout=5)
        self.assertTrue(disconnected.wait(2))
        self.assert_gone(capture['child'])

    def test_client_failure_cleanup(self):
        self.env['FAKE_FAIL'] = '1'
        process, capture = self.start_tunnel('30s')
        _, error = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('status 7', error)
        self.assertFalse(Path(capture['config']).exists())
        self.assert_gone(capture['child'])

    def test_live_ssh_over_verified_tls_websocket(self):
        for name in ('host', 'client', 'wrong'):
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(self.root/name)],check=True)
        (self.root/'authorized_keys').write_text((self.root/'client.pub').read_text())
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); ssh_port=sock.getsockname()[1]
        config = self.root/'sshd_config'
        config.write_text(f'''Port {ssh_port}
ListenAddress 127.0.0.1
HostKey {self.root}/host
AuthorizedKeysFile {self.root}/authorized_keys
PidFile none
UsePAM no
StrictModes no
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
AllowUsers {pwd.getpwuid(os.getuid()).pw_name}
''')
        daemon = subprocess.Popen(['/usr/sbin/sshd','-D','-e','-f',str(config)],stderr=subprocess.PIPE)
        self.addCleanup(daemon.stderr.close)
        self.addCleanup(lambda: daemon.poll() is None and (daemon.terminate(), daemon.wait(timeout=3)))
        def listening():
            if daemon.poll() is not None:
                self.fail(daemon.stderr.read().decode())
            try:
                with socket.create_connection(('127.0.0.1',ssh_port),timeout=.2): return True
            except OSError: return False
        wait_for(listening)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(self.root/'tls.key'),
                        '-out',str(self.root/'tls.crt'),'-days','1','-subj','/CN=localhost',
                        '-addext','subjectAltName=DNS:localhost'],check=True,capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.root/'tls.crt', self.root/'tls.key')
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever,daemon=True); thread.start()
        self.addCleanup(lambda: (loop.call_soon_threadsafe(loop.stop), thread.join(), loop.close()))
        async def start_server():
            return await serve(lambda ws: tunnel_tcp.relay(ws,ssh_port), '127.0.0.1', 0, ssl=context,
                               origins=[None],compression=None,close_timeout=1)
        server = asyncio.run_coroutine_threadsafe(start_server(),loop).result()
        async def close_server():
            server.close(); await server.wait_closed()
        self.addCleanup(lambda: asyncio.run_coroutine_threadsafe(close_server(),loop).result(timeout=4))
        ws_port = server.sockets[0].getsockname()[1]
        known = self.root/'known_hosts'
        known.write_text('localhost '+(self.root/'host.pub').read_text())
        environment = dict(self.env, SSL_CERT_FILE=str(self.root/'tls.crt'))
        command = ['ssh','-F','/dev/null','-o','BatchMode=yes','-o','ConnectTimeout=5','-o','IdentitiesOnly=yes',
                   '-o','StrictHostKeyChecking=yes','-o',f'UserKnownHostsFile={known}',
                   '-o',f'ProxyCommand={ROOT}/tunnel connect https://localhost:{ws_port}']
        ok = subprocess.run(command+['-i',str(self.root/'client'),'localhost','printf tunnel-ssh-ok'],
                            env=environment,capture_output=True,text=True,timeout=10)
        self.assertEqual(ok.returncode,0,ok.stderr)
        self.assertEqual(ok.stdout,'tunnel-ssh-ok')
        bad = subprocess.run(command+['-i',str(self.root/'wrong'),'localhost','true'],
                             env=environment,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(bad.returncode,0)
        self.assertIn('Permission denied',bad.stderr)
        known.write_text('localhost '+(self.root/'wrong.pub').read_text())
        changed = subprocess.run(command+['-i',str(self.root/'client'),'localhost','true'],
                                 env=environment,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(changed.returncode,0)
        self.assertIn('HOST IDENTIFICATION HAS CHANGED',changed.stderr)

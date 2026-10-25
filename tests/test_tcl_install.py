"""Exercise fragmented T-Cast replies and terminal installation outcomes."""
import hashlib
import importlib.util
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen

SPEC = importlib.util.spec_from_file_location(
    'tcl_install', Path(__file__).resolve().parents[1] / 'tools/tcl-install.py')
tcl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tcl)


def frame(text):
    data = text.encode()
    return struct.pack('>I', len(data)) + data


class FakeSocket:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.sent.extend(tcl.messages(data)[0])

    def recv(self, size):
        reply = next(self.replies, b'')
        if isinstance(reply, Exception):
            raise reply
        return reply


class TCastInstallTests(unittest.TestCase):
    url = 'http://127.0.0.1:18765/CleanYourTV.apk'

    def reply(self, kind, status, package=tcl.PACKAGE, token=None):
        token = token or hashlib.md5(self.url.encode()).hexdigest()
        fields = ['223', kind, package, 'CleanYourTV', status]
        if kind == 'PROGRESS':
            fields.append('100')
        return frame('>>'.join(fields + [token]))

    def run_install(self, sock):
        with patch.object(tcl.socket, 'create_connection', return_value=sock):
            tcl.install('192.168.1.50', self.url, '7', '0.45', 10)

    def test_fragmented_confirmation_then_success(self):
        data = self.reply('INSTALL', 'confirm_pending') + self.reply('PROGRESS', '5')
        sock = FakeSocket([data[:2], data[2:17], data[17:]])
        self.run_install(sock)
        self.assertTrue(sock.closed)
        self.assertEqual(sock.sent[0], '159>>1>>0')
        fields = sock.sent[1].split('>>')
        self.assertEqual(fields[1:3], ['INSTALL', tcl.PACKAGE])
        self.assertEqual(fields[5:8], ['2', '7', '0.45'])

    def test_ignores_other_package_and_stale_request(self):
        sock = FakeSocket([self.reply('PROGRESS', '5', package='other.app') +
                           self.reply('PROGRESS', '5', token='stale')])
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            self.run_install(sock)

    def test_denial_and_failure_close_connection(self):
        for kind, status in [('INSTALL', 'denied'), ('PROGRESS', '2'), ('PROGRESS', '4')]:
            with self.subTest(status=status):
                sock = FakeSocket([self.reply(kind, status)])
                with self.assertRaises(RuntimeError):
                    self.run_install(sock)
                self.assertTrue(sock.closed)

    def test_installed_acknowledgement(self):
        self.run_install(FakeSocket([self.reply('INSTALL', 'installed')]))

    def test_heartbeat_keeps_pending_install_alive(self):
        sock = FakeSocket([socket.timeout(), self.reply('PROGRESS', '5')])
        with patch.object(tcl.time, 'monotonic', side_effect=[0, 0, 1, 6, 6, 7]):
            self.run_install(sock)
        self.assertIn('150>>', sock.sent)

    def test_timeout_closes_connection(self):
        sock = FakeSocket([])
        with patch.object(tcl.time, 'monotonic', side_effect=[0, 0, 11]):
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                self.run_install(sock)
        self.assertTrue(sock.closed)

    def test_invalid_length(self):
        for size in (0, tcl.MAX_FRAME + 1, 0xffffffff):
            with self.assertRaises(ValueError):
                tcl.messages(struct.pack('>I', size))

    def test_tunnel_and_server_cleaned_after_failure(self):
        servers = []
        server_type = tcl.http.server.ThreadingHTTPServer

        def create_server(*args):
            server = server_type(*args)
            servers.append(server)
            return server

        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / 'preview.apk'
            apk.write_bytes(b'local-apk')
            with patch.object(tcl.http.server, 'ThreadingHTTPServer', side_effect=create_server), \
                    patch.object(tcl.subprocess, 'run') as run, \
                    patch.object(tcl.subprocess, 'check_output', return_value='9876\n'):
                with self.assertRaisesRegex(RuntimeError, 'declined'):
                    with tcl.apk_url(apk, 'adb', '192.168.1.50'):
                        url = f'http://127.0.0.1:{servers[0].server_port}/CleanYourTV.apk'
                        with urlopen(url) as response:
                            self.assertEqual(response.read(), b'local-apk')
                        raise RuntimeError('declined')
                run.assert_called_with(['adb', '-s', '192.168.1.50:5555', 'reverse',
                                        '--remove', 'tcp:9876'], capture_output=True)
                self.assertEqual(servers[0].socket.fileno(), -1)

    def test_apk_home_filter_rejected_but_query_allowed(self):
        badging = f"package: name='{tcl.PACKAGE}' versionCode='7' versionName='2.6'"
        query = '''E: manifest (line=1)
  E: queries (line=2)
    E: intent (line=3)
      E: category (line=4)
        A: android:name="android.intent.category.HOME"
  E: application (line=5)
    E: activity (line=6)
      E: intent-filter (line=7)
        E: category (line=8)
          A: android:name="android.intent.category.LAUNCHER"
'''
        with patch.object(tcl.subprocess, 'check_output', side_effect=[badging, query]):
            self.assertEqual(tcl.apk_info(Path('preview.apk'), 'aapt'), '7')
        with patch.object(tcl.subprocess, 'check_output', side_effect=[
                badging, query.replace('category.LAUNCHER', 'category.HOME')]):
            with self.assertRaisesRegex(ValueError, 'declares HOME'):
                tcl.apk_info(Path('preview.apk'), 'aapt')


if __name__ == '__main__':
    unittest.main()

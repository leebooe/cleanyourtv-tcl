#!/usr/bin/env python3
"""Install the locally built preview APK through TCL MultiScreenInteraction.

Build: cd launcher && ./gradlew --no-daemon assemblePreview
Install: python3 tools/tcl-install.py --tv <tv-ip>
Requires Android SDK aapt and adb for the temporary loopback download tunnel.
With --url, the TV downloads an owner-hosted copy of the same APK instead.
The viewer confirms 同意 on the TV. Home and accessibility settings stay as configured.

Protocol reference: aerial-tv/tools/tcast-push.py (MIT), TCP 6553.
"""
import argparse
import contextlib
import hashlib
import http.server
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = 'com.gathin.tvhome.preview'
MAX_FRAME = 1024 * 1024


def send(sock, message):
    data = message.encode('utf-8')
    sock.sendall(struct.pack('>I', len(data)) + data)


def messages(buffer):
    """Decode complete frames; retain partial headers and payloads for the next read."""
    found = []
    while len(buffer) >= 4:
        size = struct.unpack('>I', buffer[:4])[0]
        if size == 0 or size > MAX_FRAME:
            raise ValueError('Invalid T-Cast frame length')
        if len(buffer) < size + 4:
            break
        found.append(buffer[4:4 + size].decode('utf-8'))
        buffer = buffer[4 + size:]
    return found, buffer


def sdk_tool(name, explicit=None):
    if explicit:
        return explicit
    if shutil.which(name):
        return shutil.which(name)
    sdk = os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT')
    if sdk:
        sdk = Path(sdk)
        candidates = ([sdk / 'platform-tools' / name] if name == 'adb'
                      else sorted(sdk.glob('build-tools/*/' + name), reverse=True))
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    raise ValueError(f'Set ANDROID_HOME or supply --{name} with the SDK tool path')


def apk_info(apk, aapt):
    badging = subprocess.check_output([aapt, 'dump', 'badging', str(apk)], text=True)
    match = re.search(r"^package: name='([^']+)' versionCode='(\d+)'", badging, re.M)
    if not match or match[1] != PACKAGE:
        raise ValueError(f'Expected {PACKAGE}; build assemblePreview first')
    tree = subprocess.check_output([aapt, 'dump', 'xmltree', str(apk),
                                    'AndroidManifest.xml'], text=True)
    # HOME also appears under <queries>; only activity intent filters claim Home.
    filter_indent = None
    for line in tree.splitlines():
        indent = len(line) - len(line.lstrip())
        if line.lstrip().startswith('E:'):
            if filter_indent is not None and indent <= filter_indent:
                filter_indent = None
            if line.strip().startswith('E: intent-filter '):
                filter_indent = indent
        if filter_indent is not None and '"android.intent.category.HOME"' in line:
            raise ValueError('This APK declares HOME; use the application preview build')
    return match[2]


def install(tv, url, version, size_mb, timeout, port=6553):
    path_md5 = hashlib.md5(url.encode()).hexdigest()
    label = 'CleanYourTV'
    with socket.create_connection((tv, port), timeout=5) as sock:
        sock.settimeout(1)
        send(sock, '159>>1>>0')
        send(sock, '>>'.join(['223', 'INSTALL', PACKAGE, label, url, '2', version,
                             size_mb, '', '', '', path_md5]))
        buffer = b''
        deadline = time.monotonic() + timeout
        last_beat = time.monotonic()
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    raise RuntimeError('The TV closed the T-Cast connection')
                buffer += chunk
            except socket.timeout:
                pass
            found, buffer = messages(buffer)
            for message in found:
                parts = message.split('>>')
                if len(parts) < 6 or parts[:1] != ['223'] or parts[2] != PACKAGE:
                    continue
                if parts[-1] != path_md5:
                    continue
                status = parts[4]
                if parts[1] == 'INSTALL':
                    if status == 'confirm_pending':
                        print('Choose 同意 (Agree) on the TV to install CleanYourTV.', flush=True)
                    elif status == 'denied':
                        raise RuntimeError('Installation declined on the TV')
                    elif status == 'installed':
                        print('The TV reports CleanYourTV installed.', flush=True)
                        return
                elif parts[1] == 'PROGRESS':
                    if status == '5':
                        print('The TV reports CleanYourTV installed.', flush=True)
                        return
                    if status in ('2', '4'):
                        raise RuntimeError(f'The TV rejected download/installation (stage {status})')
            if time.monotonic() - last_beat >= 5:
                send(sock, '150>>')
                last_beat = time.monotonic()
        raise RuntimeError('Timed out waiting for installation on the TV')


class ApkHandler(http.server.BaseHTTPRequestHandler):
    """Serve exactly one APK through loopback; keep other workspace files private."""
    def do_GET(self):
        if self.path != '/CleanYourTV.apk':
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/vnd.android.package-archive')
        self.send_header('Content-Length', str(self.server.apk.stat().st_size))
        self.end_headers()
        with self.server.apk.open('rb') as source:
            shutil.copyfileobj(source, self.wfile)

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def apk_url(apk, adb, tv):
    serial = tv + ':5555'
    subprocess.run([adb, 'connect', serial], check=True, capture_output=True)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), ApkHandler)
    server.apk = apk
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    remote = None
    try:
        # Let adb allocate an unused TV port; preserve every existing reverse mapping.
        result = subprocess.check_output([adb, '-s', serial, 'reverse', 'tcp:0',
                                          f'tcp:{server.server_port}'], text=True).strip()
        if not result.isdigit() or not 0 < int(result) < 65536:
            raise RuntimeError('ADB did not allocate a reverse port')
        remote = 'tcp:' + result
        yield f'http://127.0.0.1:{result}/CleanYourTV.apk'
    finally:
        if remote:
            subprocess.run([adb, '-s', serial, 'reverse', '--remove', remote],
                           capture_output=True)
        server.shutdown()
        thread.join()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('apk', nargs='?', type=Path,
                        default=ROOT / 'launcher/app/build/outputs/apk/preview/app-preview.apk')
    parser.add_argument('--tv', default=os.environ.get('TV_HOST'))
    parser.add_argument('--aapt')
    parser.add_argument('--adb')
    parser.add_argument('--url', help='TV-reachable URL serving this exact APK; works without ADB')
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    if not args.tv or args.timeout <= 0:
        parser.error('Supply --tv <tv-ip> and a positive timeout')
    try:
        apk = args.apk.resolve(strict=True)
        version = apk_info(apk, sdk_tool('aapt', args.aapt))
        size_mb = f'{apk.stat().st_size / (1024 * 1024):.3f}'
        if args.url:
            parsed = urlsplit(args.url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or '>>' in args.url:
                raise ValueError('Supply an HTTP(S) APK URL')
            install(args.tv, args.url, version, size_mb, args.timeout)
        else:
            with apk_url(apk, sdk_tool('adb', args.adb), args.tv) as url:
                install(args.tv, url, version, size_mb, args.timeout)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Install stopped: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-

# SlopBro
# by throwaway96
# https://github.com/throwaway96/slopbro
# Copyright 2026. Licensed under AGPL v3 or later. No warranties.

"""
slopbro.py - Connect to an LG webOS TV over SSAP, pair, and
launch various apps with a self-hosted payload page.

Single file, standard library only. Works on Python 2.7 and Python 3.x, so it can
run on a PC on the LAN or directly on the TV.

Example:
    python slopbro.py 192.168.1.50
    python slopbro.py --debug 192.168.1.50
    python slopbro.py --local-ip 1.2.3.4 192.168.1.50
"""

from __future__ import print_function

import base64
import io
import json
import os
import posixpath
import socket
import ssl
import struct
import sys
import threading
import time
from hashlib import sha1

try:
    from http.server import HTTPServer, SimpleHTTPRequestHandler
except ImportError:
    try:
        from BaseHTTPServer import HTTPServer
        from SimpleHTTPServer import SimpleHTTPRequestHandler
    except ImportError:
        HTTPServer = None
        SimpleHTTPRequestHandler = None

try:
    from urllib.parse import unquote, urlsplit
except ImportError:
    from urllib import unquote
    from urlparse import urlsplit

# os.urandom is available on all target versions; used for masking + nonce.
_urandom = os.urandom

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

PORT_TLS = 3001
PORT_PLAIN = 3000

TARGET_APPS = [
    ("com.webos.app.dangbei-overlay", "target"),
    ("com.webos.app.adoverlay", "interactiveUrl"),
    ("com.webos.app.acroverlay", "contentTarget"),
    ("com.webos.app.tinybrowser", "contentTarget"),
]

ENTRY_PAGE = "index.html"
SHELL_SCRIPT = "autoroot.sh"
SERVICE_FILES = [
    "package.json",
    "main.js",
]

ASSET_SOURCE_AUTO = "auto"
ASSET_SOURCE_DIR = "dir"
ASSET_SOURCE_EMBEDDED = "embedded"
ASSET_SOURCES = (ASSET_SOURCE_AUTO, ASSET_SOURCE_DIR, ASSET_SOURCE_EMBEDDED)

# --- BEGIN EMBEDDED WWWROOT ---
EMBEDDED_WWWROOT = {}
# --- END EMBEDDED WWWROOT ---

# seconds
CONNECT_TIMEOUT = 10.0
PAIR_TIMEOUT = 60.0

# Manifest distilled from the reference clients. Broad permission set so the
# launcher works across firmware versions; the TV grants what it recognizes.
DEFAULT_MANIFEST = {
    "manifestVersion": 1,
    "appVersion": "1.0",
    "appId": "lol.slopbro",
    "vendorId": "throwaway96",
    "localizedAppNames": {"": "SlopBro"},
    "localizedVendorNames": {"": "throwaway96"},
    "permissions": [
        "LAUNCH",
        "CLOSE",
        "READ_APP_STATUS",
        "READ_INSTALLED_APPS",
        "READ_RUNNING_APPS",
    ],
}


def log(msg):
    """Print a timestamped status line to stderr so stdout stays parse-friendly."""
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def die(msg, code=1):
    log("error: " + msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Minimal RFC 6455 WebSocket client (text frames only, client-side masking).
# ---------------------------------------------------------------------------


class WebSocketError(Exception):
    pass


class WebSocket(object):
    """A tiny blocking WebSocket client over a raw (optionally TLS) socket.

    Only what SSAP needs: a client handshake with no Origin header, masked text
    frames, ping/pong handling, and a clean close. Not a general-purpose library.
    """

    OP_TEXT = 0x1
    OP_CLOSE = 0x8
    OP_PING = 0x9
    OP_PONG = 0xA

    def __init__(self, sock):
        self._sock = sock
        self._recv_buf = b""
        self.closed = False

    @classmethod
    def connect(cls, host, port, secure=True, timeout=CONNECT_TIMEOUT):
        raw = socket.create_connection((host, port), timeout)
        if secure:
            proto = getattr(ssl, "PROTOCOL_TLS_CLIENT", None)
            if proto is None:
                proto = getattr(ssl, "PROTOCOL_SSLv23", ssl.PROTOCOL_TLS)
            context = ssl.SSLContext(proto)
            # Intentional: the TV uses a self-signed certificate.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            sock = context.wrap_socket(raw, server_hostname=host)
        else:
            sock = raw
        ws = cls(sock)
        ws._handshake(host, port, secure)
        return ws

    def _handshake(self, host, port, secure):
        key = base64.b64encode(_urandom(16)).decode("ascii")
        # Deliberately no Origin header: that is what lets a native client past
        # the SSAP server's web-origin filter.
        lines = [
            "GET / HTTP/1.1",
            "Host: %s:%d" % (host, port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
            "",
            "",
        ]
        self._sock.sendall("\r\n".join(lines).encode("ascii"))

        header = self._read_http_headers()
        status_line = header.split("\r\n", 1)[0]
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or parts[1] != "101":
            raise WebSocketError("unexpected handshake response: %s" % status_line)

        expected = base64.b64encode(
            sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        accept = None
        for line in header.split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                if name.strip().lower() == "sec-websocket-accept":
                    accept = value.strip()
                    break
        if accept != expected:
            raise WebSocketError("invalid Sec-WebSocket-Accept value")

    def _read_http_headers(self):
        while b"\r\n\r\n" not in self._recv_buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            self._recv_buf += chunk
        header_bytes, self._recv_buf = self._recv_buf.split(b"\r\n\r\n", 1)
        return header_bytes.decode("latin-1")

    def _recv_exact(self, n):
        while len(self._recv_buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._recv_buf)))
            if not chunk:
                raise WebSocketError("connection closed")
            self._recv_buf += chunk
        data, self._recv_buf = self._recv_buf[:n], self._recv_buf[n:]
        return data

    def send_text(self, text):
        self._send_frame(self.OP_TEXT, text.encode("utf-8"))

    def _send_frame(self, opcode, payload):
        if self.closed:
            raise WebSocketError("socket is closed")
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode
        length = len(payload)
        mask_bit = 0x80  # client frames must be masked
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", length)
        mask = bytearray(_urandom(4))
        header += mask
        masked = bytearray(payload)
        for i in range(len(masked)):
            masked[i] ^= mask[i % 4]
        self._sock.sendall(bytes(header) + bytes(masked))

    def recv_text(self, timeout=None):
        """Return the next text-frame payload as a unicode string.

        Transparently answers pings. Returns
        None if the peer sends a close frame.
        """
        if timeout is not None:
            self._sock.settimeout(timeout)
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == self.OP_TEXT:
                return payload.decode("utf-8")
            elif opcode == self.OP_PING:
                self._send_frame(self.OP_PONG, payload)
                continue
            elif opcode == self.OP_PONG:
                continue
            elif opcode == self.OP_CLOSE:
                self._send_close()
                self.closed = True
                return None
            # Ignore unexpected non-text frames.
            if not fin:
                continue

    def _read_frame(self):
        b0, b1 = struct.unpack("!BB", self._recv_exact(2))
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b""
        if mask:
            payload = bytearray(payload)
            for i in range(len(payload)):
                payload[i] ^= mask[i % 4]
            payload = bytes(payload)
        return fin, opcode, payload

    def _send_close(self):
        try:
            self._send_frame(self.OP_CLOSE, b"")
        except (WebSocketError, socket.error):
            pass

    def close(self):
        if not self.closed:
            self._send_close()
            self.closed = True
        try:
            self._sock.close()
        except socket.error:
            pass


# ---------------------------------------------------------------------------
# SSAP client layer
# ---------------------------------------------------------------------------


class SSAPError(Exception):
    pass


class SSAPClient(object):
    def __init__(self, ws):
        self._ws = ws
        self._counter = 0

    def _next_id(self, prefix):
        self._counter += 1
        return "%s_%d" % (prefix, self._counter)

    def _send(self, message):
        self._ws.send_text(json.dumps(message))

    def register(self, client_key, manifest, timeout=PAIR_TIMEOUT):
        """Perform the SSAP register handshake.

        Returns the client-key string from the 'registered' reply. If no key was
        stored, the TV shows a pairing prompt and this blocks (up to timeout)
        until the user accepts.
        """
        payload = {
            "forcePairing": False,
            "pairingType": "PROMPT",
            "manifest": manifest,
        }
        if client_key:
            payload["client-key"] = client_key
        msg_id = self._next_id("register")
        self._send({"id": msg_id, "type": "register", "payload": payload})

        prompted = False
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise SSAPError("timed out waiting for registration")
            try:
                raw = self._ws.recv_text(timeout=remaining)
            except socket.timeout:
                raise SSAPError("timed out waiting for registration")
            if raw is None:
                raise SSAPError("connection closed during registration")
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "registered":
                return (msg.get("payload") or {}).get("client-key", client_key)
            if mtype == "response" and (msg.get("payload") or {}).get("pairingType"):
                if not prompted:
                    prompted = True
                    log("accept the pairing prompt on the TV screen ...")
                continue
            if mtype == "error":
                raise SSAPError(msg.get("error") or "registration rejected")
            # Ignore anything else (e.g. stray notifications) and keep waiting.

    def request(self, uri, payload=None, timeout=15.0):
        """Send an SSAP request and return the matching response message."""
        msg_id = self._next_id("req")
        self._send(
            {
                "id": msg_id,
                "type": "request",
                "uri": uri,
                "payload": payload or {},
            }
        )
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise SSAPError("timed out waiting for response to %s" % uri)
            try:
                raw = self._ws.recv_text(timeout=remaining)
            except socket.timeout:
                raise SSAPError("timed out waiting for response to %s" % uri)
            if raw is None:
                raise SSAPError("connection closed while awaiting %s" % uri)
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                return msg
            # Different id: not ours, keep reading.

    def close(self):
        self._ws.close()


# ---------------------------------------------------------------------------
# Client-key persistence
# ---------------------------------------------------------------------------


def key_path(ip):
    return os.path.join(os.getcwd(), "%s.key" % ip)


def load_client_key(ip):
    path = key_path(ip)
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except (IOError, OSError):
        return ""


def save_client_key(ip, client_key):
    path = key_path(ip)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        try:
            os.makedirs(directory)
        except OSError:
            pass
    try:
        with open(path, "w") as fh:
            fh.write(client_key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except (IOError, OSError) as exc:
        log("warning: could not save client key to %s (%s)" % (path, exc))


# ---------------------------------------------------------------------------
# High-level flow
# ---------------------------------------------------------------------------


def verify_app_present(client, candidates):
    """Check installed apps and return the first matching (app_id, url_param_name).

    Falls back to the first candidate if the check fails or no match is found.
    """
    fallback = candidates[0]
    try:
        resp = client.request(
            "ssap://com.webos.applicationManager/listApps", {}, timeout=8.0
        )
    except SSAPError as exc:
        log("warning: could not verify app presence (%s)" % exc)
        return fallback
    if resp.get("type") == "error":
        log(
            "warning: listApps denied (%s); skipping app check"
            % resp.get("error", "unknown")
        )
        return fallback
    apps = (resp.get("payload") or {}).get("apps") or []
    installed_ids = set(
        a.get("id") for a in apps if isinstance(a, dict) and a.get("id")
    )
    for app_id, url_param_name in candidates:
        if app_id in installed_ids:
            log("confirmed app is installed: %s" % app_id)
            return app_id, url_param_name
    log(
        "warning: none of [%s] found in listApps; launch may do nothing"
        % ", ".join(a for a, _ in candidates)
    )
    return fallback


def launch_app(client, app_id, params):
    resp = client.request(
        "ssap://system.launcher/launch", {"id": app_id, "params": params}
    )
    if resp.get("type") == "error":
        raise SSAPError("launch failed: %s" % resp.get("error", "unknown"))
    return resp.get("payload") or {}


def local_ip_for_remote(remote_host, remote_port=80):
    """Choose a LAN-reachable local IPv4 for a given remote host."""
    remote_ip = socket.gethostbyname(remote_host)
    route_ip = _route_selected_local_ip(remote_ip, remote_port)

    # LunaDownloadMgr seems to choke on 127.0.0.1
    if route_ip and route_ip.startswith("127."):
        # Arbitrary external address; doesn't have to be reachable
        lan_ip = _route_selected_local_ip("8.8.8.8", 80)
        if lan_ip and not lan_ip.startswith("127."):
            log("detected loopback route; using LAN IP %s instead" % lan_ip)
            return lan_ip

    if route_ip:
        return route_ip
    raise RuntimeError("could not determine local IPv4 address")


def _route_selected_local_ip(remote_ip, remote_port):
    """Ask the kernel route table which source IP it would pick."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_ip, remote_port))
        return sock.getsockname()[0]
    except socket.error:
        return None
    finally:
        sock.close()


def required_files():
    return [ENTRY_PAGE, SHELL_SCRIPT] + list(SERVICE_FILES)


def resolve_wwwroot_path():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.join(script_dir, "wwwroot")
    return root_dir


def _decode_embedded_data(encoded):
    if not isinstance(encoded, bytes):
        encoded = encoded.encode("ascii")
    return base64.b64decode(encoded)


def _embedded_meta_for(rel_path):
    if not isinstance(EMBEDDED_WWWROOT, dict):
        return None
    meta = EMBEDDED_WWWROOT.get(rel_path)
    if not isinstance(meta, dict):
        return None
    return meta


def _read_embedded_file(rel_path):
    meta = _embedded_meta_for(rel_path)
    if meta is None:
        return None
    encoding = meta.get("encoding", "base64")
    if encoding != "base64":
        raise RuntimeError(
            "unsupported embedded encoding for %s: %s" % (rel_path, encoding)
        )
    payload = meta.get("data", "")
    return _decode_embedded_data(payload)


def _read_filesystem_file(root_dir, rel_path):
    if not root_dir:
        return None
    abs_path = os.path.join(root_dir, rel_path.replace("/", os.sep))
    if not os.path.isfile(abs_path):
        return None
    with open(abs_path, "rb") as fh:
        return fh.read()


def _missing_required_files(
    tracked_files,
    root_dir,
    allow_embedded_assets,
    allow_filesystem_fallback,
):
    missing = []
    for rel_path in tracked_files:
        if allow_embedded_assets and _embedded_meta_for(rel_path) is not None:
            continue
        if (
            allow_filesystem_fallback
            and _read_filesystem_file(root_dir, rel_path) is not None
        ):
            continue
        missing.append(rel_path)
    return missing


def resolve_asset_root(asset_source, tracked_files):
    dir_root = resolve_wwwroot_path()
    if asset_source not in ASSET_SOURCES:
        raise RuntimeError(
            "invalid --asset-source '%s' (expected one of: %s)"
            % (asset_source, ", ".join(ASSET_SOURCES))
        )

    allow_embedded_assets = asset_source != ASSET_SOURCE_DIR
    allow_filesystem_fallback = asset_source != ASSET_SOURCE_EMBEDDED
    if allow_filesystem_fallback and not os.path.isdir(dir_root):
        dir_root = None

    missing = _missing_required_files(
        tracked_files,
        dir_root,
        allow_embedded_assets,
        allow_filesystem_fallback,
    )
    if missing:
        raise RuntimeError(
            "required files not found in %s: %s"
            % (
                (
                    "wwwroot"
                    if asset_source == ASSET_SOURCE_DIR
                    else (
                        "embedded data"
                        if asset_source == ASSET_SOURCE_EMBEDDED
                        else "embedded data or wwwroot"
                    )
                ),
                ", ".join(missing),
            )
        )

    if asset_source == ASSET_SOURCE_DIR:
        source_desc = ASSET_SOURCE_DIR
    elif allow_filesystem_fallback and dir_root is not None:
        source_desc = "embedded+dir"
    elif allow_filesystem_fallback:
        source_desc = "embedded"
    else:
        source_desc = ASSET_SOURCE_EMBEDDED
    return dir_root, source_desc, allow_embedded_assets, allow_filesystem_fallback


class RequestedFilesTracker(object):
    def __init__(self, tracked_files):
        self._tracked = set(tracked_files)
        self._seen = set()
        self._lock = threading.Lock()
        self._complete = threading.Event()
        if not self._tracked:
            self._complete.set()

    def mark_seen(self, rel_path):
        with self._lock:
            if rel_path not in self._tracked or rel_path in self._seen:
                return False, len(self._seen), len(self._tracked)
            self._seen.add(rel_path)
            if self._seen == self._tracked:
                self._complete.set()
            return True, len(self._seen), len(self._tracked)

    def wait_for_all(self, timeout):
        return self._complete.wait(timeout)

    def missing_files(self):
        with self._lock:
            return sorted(self._tracked - self._seen)

    def total_files(self):
        with self._lock:
            return len(self._tracked)


def make_tracking_handler(
    root_dir,
    tracker,
    tracked_files,
    allow_embedded_assets,
    allow_filesystem_fallback,
):
    if SimpleHTTPRequestHandler is None:
        raise RuntimeError("no stdlib HTTP request handler available")

    tracked = set(tracked_files)

    class TrackingHTTPRequestHandler(SimpleHTTPRequestHandler):
        def _requested_rel_path(self):
            raw_path = urlsplit(self.path).path
            try:
                raw_path = unquote(raw_path, errors="surrogatepass")
            except TypeError:
                raw_path = unquote(raw_path)
            raw_path = posixpath.normpath(raw_path)
            if raw_path in ("", "/", "."):
                return ENTRY_PAGE
            rel_path = raw_path.lstrip("/")
            if rel_path.startswith("../") or rel_path == "..":
                return None
            return rel_path.replace("\\", "/")

        def _send_bytes(self, rel_path, data):
            ctype = self.guess_type(rel_path)
            self.send_response(200)
            self.send_header("Content-type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return io.BytesIO(data)

        def _mark_served_file(self, rel_path):
            marked, seen_count, total_count = tracker.mark_seen(rel_path)
            if marked:
                log(
                    "served required file: %s (%d/%d)"
                    % (
                        rel_path,
                        seen_count,
                        total_count,
                    )
                )

        def send_head(self):
            rel_path = self._requested_rel_path()
            if rel_path is None or rel_path not in tracked:
                self.send_error(404, "File not found")
                return None

            data = None
            if allow_embedded_assets:
                data = _read_embedded_file(rel_path)
            if data is None and allow_filesystem_fallback:
                data = _read_filesystem_file(root_dir, rel_path)
            if data is None:
                self.send_error(404, "File not found")
                return None

            self._mark_served_file(rel_path)
            return self._send_bytes(rel_path, data)

    return TrackingHTTPRequestHandler


def start_http_server(
    root_dir,
    tracker,
    tracked_files,
    allow_embedded_assets,
    allow_filesystem_fallback,
    bind_host="0.0.0.0",
    preferred_port=8080,
):
    """Start a tiny static file server rooted at root_dir."""
    handler = make_tracking_handler(
        root_dir,
        tracker,
        tracked_files,
        allow_embedded_assets,
        allow_filesystem_fallback,
    )
    server = None
    for port in (preferred_port, 0):
        try:
            server = HTTPServer((bind_host, port), handler)
            break
        except socket.error:
            server = None
    if server is None:
        raise RuntimeError("could not start local HTTP server")

    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server


def build_self_hosted_url(tv_host, server_port, debug=False, local_ip_override=None):
    local_ip = local_ip_override or local_ip_for_remote(tv_host)
    url = "http://%s:%d/%s" % (local_ip, server_port, ENTRY_PAGE)
    url += "?script=%s&files=%s" % (SHELL_SCRIPT, ";".join(SERVICE_FILES))
    if debug:
        url += "&debug"
    return url


def _validate_ipv4_address(ip_text):
    try:
        socket.inet_aton(ip_text)
    except socket.error:
        return False
    return ip_text.count(".") == 3


def run(
    host,
    debug=False,
    asset_source=ASSET_SOURCE_AUTO,
    local_ip_override=None,
):
    secure = True
    port = PORT_TLS if secure else PORT_PLAIN
    scheme = "wss" if secure else "ws"
    log("connecting to %s://%s:%d" % (scheme, host, port))

    tracked_files = required_files()
    wwwroot_dir = None
    asset_source_used = None
    allow_embedded_assets = True
    allow_filesystem_fallback = True
    http_server = None
    page_url = None
    ws = None
    new_key = ""
    result = {}
    try:
        (
            wwwroot_dir,
            asset_source_used,
            allow_embedded_assets,
            allow_filesystem_fallback,
        ) = resolve_asset_root(
            asset_source,
            tracked_files,
        )
    except RuntimeError as exc:
        die("%s" % exc)

    tracker = RequestedFilesTracker(tracked_files)
    if tracked_files:
        log(
            "tracking %d files in %s (source=%s)"
            % (len(tracked_files), wwwroot_dir, asset_source_used)
        )
    else:
        log("warning: no files found in %s" % wwwroot_dir)
    try:
        http_server = start_http_server(
            wwwroot_dir,
            tracker,
            tracked_files,
            allow_embedded_assets,
            allow_filesystem_fallback,
        )
        page_url = build_self_hosted_url(
            host,
            http_server.server_port,
            debug=debug,
            local_ip_override=local_ip_override,
        )
    except Exception as exc:
        die("could not start self-hosted page server: %s" % exc)
    log("serving files from %s at %s" % (wwwroot_dir, page_url))

    try:
        ws = WebSocket.connect(
            host,
            port,
            secure=secure,
        )
    except (socket.error, ssl.SSLError, WebSocketError) as exc:
        die("could not connect to TV: %s" % exc)

    client = SSAPClient(ws)
    try:
        stored_key = load_client_key(host)
        if stored_key:
            log("using stored client key")
        else:
            log("no stored client key; pairing will be requested")

        try:
            new_key = client.register(stored_key, DEFAULT_MANIFEST)
        except SSAPError as exc:
            die("registration failed: %s" % exc)

        if new_key and new_key != stored_key:
            save_client_key(host, new_key)
            log("registered; client key saved")
        else:
            log("registered")

        app_id, url_param_name = verify_app_present(client, TARGET_APPS)

        log("launching %s -> %s" % (app_id, page_url))
        try:
            result = launch_app(client, app_id, {url_param_name: page_url})
        except SSAPError as exc:
            die("%s" % exc)

        print(json.dumps(result, sort_keys=True))

        log("waiting for all tracked files to be requested")
        tracker.wait_for_all(None)
        log("done; all tracked files were requested (❛ ᴗ ❛)")
    finally:
        client.close()
        if http_server is not None:
            try:
                http_server.shutdown()
            except Exception:
                pass
            try:
                http_server.server_close()
            except Exception:
                pass


def usage():
    print(
        "usage: python %s [--debug] "
        "[--asset-source auto|dir|embedded] "
        "[--local-ip <ipv4>] <tv-ip-or-host>" % sys.argv[0]
    )
    print(
        "example: python %s --debug --asset-source auto "
        "--local-ip 192.168.1.100 192.168.1.50" % sys.argv[0]
    )


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    debug = False
    asset_source = ASSET_SOURCE_AUTO
    local_ip_override = None
    positional = []
    index = 0

    # No argparse :(
    while index < len(argv):
        arg = argv[index]
        if arg == "--debug":
            debug = True
            index += 1
        elif arg == "--local-ip":
            if index + 1 >= len(argv):
                print("error: missing value for --local-ip")
                usage()
                return 2
            local_ip_override = argv[index + 1]
            index += 2
        elif arg.startswith("--local-ip="):
            local_ip_override = arg.split("=", 1)[1]
            index += 1
        elif arg == "--asset-source":
            if index + 1 >= len(argv):
                print("error: missing value for --asset-source")
                usage()
                return 2
            asset_source = argv[index + 1]
            index += 2
        elif arg.startswith("--asset-source="):
            asset_source = arg.split("=", 1)[1]
            index += 1
        elif arg.startswith("-"):
            print("error: unknown option %s" % arg)
            usage()
            return 2
        else:
            positional.append(arg)
            index += 1

    if asset_source not in ASSET_SOURCES:
        print("error: invalid --asset-source value '%s'" % asset_source)
        usage()
        return 2

    if local_ip_override and not _validate_ipv4_address(local_ip_override):
        print("error: invalid --local-ip value '%s'" % local_ip_override)
        usage()
        return 2

    if not positional:
        usage()
        return 2
    if len(positional) > 1:
        print("error: too many arguments")
        usage()
        return 2

    host = positional[0]
    run(
        host,
        debug=debug,
        asset_source=asset_source,
        local_ip_override=local_ip_override,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

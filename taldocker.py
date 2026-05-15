#!/usr/bin/env python3
"""
Talentia Docker Viewer - single-file Docker UI for WSL2 (no Docker Desktop required).

Run inside WSL (where Docker daemon lives):
    python3 taldocker.py

Then open from Windows browser: http://localhost:8765
(WSL2 forwards localhost automatically.)
Override with TALDOCKER_PORT=NNNN if needed.

Security: binds 127.0.0.1 only. Docker socket has full root-equivalent
power - do not expose this port to the network.

Requires: Python 3.8+ stdlib only. User must be in the 'docker' group
(or run as root) to access /var/run/docker.sock.
"""

import argparse
import atexit
import base64
import hashlib
import http.server
import json
import os
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser


# =============================================================================
# DockerClient - HTTP/1.1 over Unix domain socket
# =============================================================================

class DockerError(Exception):
    def __init__(self, status, message):
        super().__init__(f"Docker API {status}: {message}")
        self.status = status
        self.message = message


class DockerClient:
    DEFAULT_SOCK = "/var/run/docker.sock"
    API_VERSION = "v1.41"

    def __init__(self, sock_path=None):
        self.sock_path = sock_path or os.environ.get("DOCKER_SOCKET", self.DEFAULT_SOCK)

    # ---- low-level transport ----

    def _connect(self, timeout=60):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.sock_path)
        except FileNotFoundError:
            raise DockerError(0, f"Docker socket not found at {self.sock_path}. Is the Docker daemon running?")
        except PermissionError:
            raise DockerError(0, f"Permission denied on {self.sock_path}. Run: sudo usermod -aG docker $USER && newgrp docker")
        except OSError as e:
            raise DockerError(0, f"Cannot connect to Docker: {e}")
        return s

    def _build_request(self, method, path, headers=None, body=None):
        if not path.startswith("/v"):
            path = "/" + self.API_VERSION + path
        h = {"Host": "docker", "User-Agent": "Talentia Docker Viewer", "Accept": "*/*"}
        if headers:
            h.update(headers)
        if body is not None:
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode("utf-8")
                h.setdefault("Content-Type", "application/json")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            h["Content-Length"] = str(len(body))
        else:
            body = b""
            if method in ("POST", "PUT", "DELETE"):
                h["Content-Length"] = "0"
        lines = [f"{method} {path} HTTP/1.1"]
        for k, v in h.items():
            lines.append(f"{k}: {v}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1") + body

    def _read_until(self, sock, delim, buf=b""):
        while delim not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return buf, b""
            buf += chunk
        idx = buf.find(delim)
        return buf[:idx], buf[idx + len(delim):]

    def _parse_headers(self, sock):
        head, rest = self._read_until(sock, b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        if not lines or not lines[0]:
            raise DockerError(0, "Empty response from Docker daemon")
        status_parts = lines[0].split(" ", 2)
        status = int(status_parts[1]) if len(status_parts) >= 2 else 0
        reason = status_parts[2] if len(status_parts) > 2 else ""
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return status, reason, headers, rest

    def _read_fixed(self, sock, n, prefetched=b""):
        buf = prefetched
        while len(buf) < n:
            chunk = sock.recv(min(65536, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        return buf

    def _iter_chunked(self, sock, prefetched=b""):
        buf = prefetched
        while True:
            while b"\r\n" not in buf:
                more = sock.recv(4096)
                if not more:
                    return
                buf += more
            line, buf = buf.split(b"\r\n", 1)
            try:
                size = int(line.strip().split(b";")[0], 16)
            except ValueError:
                return
            if size == 0:
                return
            while len(buf) < size + 2:
                more = sock.recv(65536)
                if not more:
                    return
                buf += more
            yield buf[:size]
            buf = buf[size + 2:]

    def _raise_error(self, status, body_bytes):
        msg = body_bytes.decode("utf-8", "replace") if body_bytes else ""
        try:
            parsed = json.loads(msg)
            msg = parsed.get("message", msg)
        except Exception:
            pass
        raise DockerError(status, msg or "(no message)")

    # ---- public modes: request / stream / hijack ----

    def request(self, method, path, body=None, query=None):
        sock = self._connect()
        try:
            if query:
                path = path + "?" + urllib.parse.urlencode(query)
            sock.sendall(self._build_request(method, path, {"Connection": "close"}, body))
            status, reason, headers, rest = self._parse_headers(sock)
            te = headers.get("transfer-encoding", "")
            cl = headers.get("content-length")
            if "chunked" in te:
                body_bytes = b"".join(self._iter_chunked(sock, rest))
            elif cl is not None:
                body_bytes = self._read_fixed(sock, int(cl), rest)
            else:
                body_bytes = rest
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    body_bytes += chunk
            if status >= 400:
                self._raise_error(status, body_bytes)
            if not body_bytes:
                return None
            ct = headers.get("content-type", "")
            if "application/json" in ct:
                return json.loads(body_bytes.decode("utf-8"))
            return body_bytes.decode("utf-8", "replace")
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def stream(self, method, path, body=None, query=None):
        sock = self._connect(timeout=None)
        if query:
            path = path + "?" + urllib.parse.urlencode(query)
        sock.sendall(self._build_request(method, path, {"Connection": "close"}, body))
        status, reason, headers, rest = self._parse_headers(sock)
        if status >= 400:
            body_bytes = rest
            try:
                body_bytes += sock.recv(65536)
            except Exception:
                pass
            sock.close()
            self._raise_error(status, body_bytes)
        te = headers.get("transfer-encoding", "")
        try:
            if "chunked" in te:
                for chunk in self._iter_chunked(sock, rest):
                    yield chunk
            else:
                if rest:
                    yield rest
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def hijack(self, method, path, body=None):
        sock = self._connect(timeout=None)
        sock.sendall(self._build_request(
            method, path,
            {"Connection": "Upgrade", "Upgrade": "tcp", "Content-Type": "application/json"},
            body,
        ))
        status, reason, headers, rest = self._parse_headers(sock)
        if status not in (101, 200):
            body_bytes = rest
            try:
                body_bytes += sock.recv(65536)
            except Exception:
                pass
            sock.close()
            self._raise_error(status, body_bytes)
        return sock, rest

    # ---- high-level API ----

    def system_version(self):  return self.request("GET", "/version")
    def system_info(self):     return self.request("GET", "/info")
    def system_df(self):       return self.request("GET", "/system/df")
    def system_ping(self):     return self.request("GET", "/_ping")

    def containers_list(self, all=True, filters=None):
        q = {"all": "1" if all else "0"}
        if filters:
            q["filters"] = json.dumps(filters)
        return self.request("GET", "/containers/json", query=q)

    def container_inspect(self, cid):           return self.request("GET", f"/containers/{cid}/json")
    def container_start(self, cid):             return self.request("POST", f"/containers/{cid}/start")
    def container_stop(self, cid, t=10):        return self.request("POST", f"/containers/{cid}/stop", query={"t": str(t)})
    def container_restart(self, cid, t=10):     return self.request("POST", f"/containers/{cid}/restart", query={"t": str(t)})
    def container_kill(self, cid):              return self.request("POST", f"/containers/{cid}/kill")
    def container_pause(self, cid):             return self.request("POST", f"/containers/{cid}/pause")
    def container_unpause(self, cid):           return self.request("POST", f"/containers/{cid}/unpause")
    def container_rename(self, cid, name):      return self.request("POST", f"/containers/{cid}/rename", query={"name": name})

    def container_remove(self, cid, force=False, volumes=False):
        return self.request("DELETE", f"/containers/{cid}", query={
            "force": "1" if force else "0",
            "v": "1" if volumes else "0",
        })

    def container_logs(self, cid, tail="500"):
        return self.stream("GET", f"/containers/{cid}/logs", query={
            "stdout": "1", "stderr": "1", "follow": "1", "tail": tail, "timestamps": "0",
        })

    def container_create(self, name, spec):
        q = {"name": name} if name else None
        return self.request("POST", "/containers/create", body=spec, query=q)

    def containers_prune(self):
        return self.request("POST", "/containers/prune")

    def images_list(self):
        return self.request("GET", "/images/json", query={"all": "0"})

    def image_inspect(self, name):
        return self.request("GET", f"/images/{name}/json")

    def image_remove(self, name, force=False):
        return self.request("DELETE", f"/images/{name}", query={"force": "1" if force else "0"})

    def image_pull(self, ref):
        if ":" in ref and "/" not in ref.split(":")[-1]:
            from_image, tag = ref.rsplit(":", 1)
        else:
            from_image, tag = ref, "latest"
        return self.stream("POST", "/images/create", query={"fromImage": from_image, "tag": tag})

    def images_prune(self, dangling_only=True):
        f = {"dangling": ["true"]} if dangling_only else {"dangling": ["false"]}
        return self.request("POST", "/images/prune", query={"filters": json.dumps(f)})

    def volumes_list(self):                     return self.request("GET", "/volumes")
    def volume_inspect(self, name):             return self.request("GET", f"/volumes/{name}")
    def volume_create(self, name, driver="local"):
        return self.request("POST", "/volumes/create", body={"Name": name, "Driver": driver})
    def volume_remove(self, name, force=False): return self.request("DELETE", f"/volumes/{name}", query={"force": "1" if force else "0"})
    def volumes_prune(self):                    return self.request("POST", "/volumes/prune")

    def networks_list(self):                    return self.request("GET", "/networks")
    def network_inspect(self, nid):             return self.request("GET", f"/networks/{nid}")
    def network_create(self, name, driver="bridge"):
        return self.request("POST", "/networks/create", body={"Name": name, "Driver": driver, "CheckDuplicate": True})
    def network_remove(self, nid):              return self.request("DELETE", f"/networks/{nid}")
    def networks_prune(self):                   return self.request("POST", "/networks/prune")

    def network_connect(self, nid, cid, aliases=None):
        body = {"Container": cid}
        if aliases:
            body["EndpointConfig"] = {"Aliases": aliases}
        return self.request("POST", f"/networks/{nid}/connect", body=body)

    def network_disconnect(self, nid, cid, force=False):
        return self.request("POST", f"/networks/{nid}/disconnect",
                            body={"Container": cid, "Force": bool(force)})

    def exec_create(self, cid, cmd, tty=True):
        if isinstance(cmd, str):
            cmd = shlex.split(cmd) if cmd else ["/bin/sh"]
        spec = {
            "AttachStdin": True, "AttachStdout": True, "AttachStderr": True,
            "Tty": tty, "Cmd": cmd,
        }
        return self.request("POST", f"/containers/{cid}/exec", body=spec)

    def exec_start_hijack(self, exec_id, tty=True):
        return self.hijack("POST", f"/exec/{exec_id}/start", body={"Detach": False, "Tty": tty})

    def exec_resize(self, exec_id, w, h):
        return self.request("POST", f"/exec/{exec_id}/resize", query={"w": str(w), "h": str(h)})


# =============================================================================
# WebSocket helpers (RFC 6455, minimal server-side)
# =============================================================================

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def ws_handshake_bytes(key):
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode()


def ws_send_frame(sock, payload, opcode=OP_BIN):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
        if opcode == OP_BIN:
            opcode = OP_TEXT
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    sock.sendall(bytes(header) + payload)


def _ws_recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def ws_recv_frame(sock):
    hdr = _ws_recv_exact(sock, 2)
    if hdr is None:
        return None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    length = b2 & 0x7F
    if length == 126:
        ext = _ws_recv_exact(sock, 2)
        if ext is None:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = _ws_recv_exact(sock, 8)
        if ext is None:
            return None
        length = struct.unpack(">Q", ext)[0]
    mask = None
    if masked:
        mask = _ws_recv_exact(sock, 4)
        if mask is None:
            return None
    payload = b"" if length == 0 else _ws_recv_exact(sock, length)
    if payload is None:
        return None
    if masked and mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


# =============================================================================
# HTTP application
# =============================================================================

DOCKER = DockerClient()


class AppHandler(http.server.BaseHTTPRequestHandler):
    server_version = "TalentiaDockerViewer/1.0"
    protocol_version = "HTTP/1.1"

    # cache TTY mode of containers (for log demuxing)
    _tty_cache: dict = {}
    _tty_lock = threading.Lock()

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    # ---- response helpers ----

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, content, ctype="text/plain; charset=utf-8", status=200):
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)

    def _serve_static(self, path):
        rel = path[len("/static/"):]
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        full = os.path.normpath(os.path.join(base, rel))
        if not full.startswith(base) or not os.path.isfile(full):
            self.send_error(404, "Not found")
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
            ".webp": "image/webp", ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, msg, status=500):
        try:
            self._send_json({"error": msg}, status)
        except Exception:
            pass

    def _read_body_json(self):
        cl = int(self.headers.get("Content-Length") or 0)
        if cl <= 0:
            return {}
        raw = self.rfile.read(cl)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _send_sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        # No Content-Length -> server will close on done; client EventSource handles that.
        self.end_headers()

    def _sse_send(self, data, event=None):
        try:
            buf = b""
            if event:
                buf += f"event: {event}\n".encode("utf-8")
            text = data if isinstance(data, str) else json.dumps(data)
            for line in text.split("\n"):
                buf += f"data: {line}\n".encode("utf-8")
            buf += b"\n"
            self.wfile.write(buf)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # ---- routing ----

    def do_GET(self):    self._dispatch("GET")
    def do_POST(self):   self._dispatch("POST")
    def do_DELETE(self): self._dispatch("DELETE")

    def _dispatch(self, method):
        try:
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            qs = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))

            if (self.headers.get("Upgrade", "").lower() == "websocket"
                    and path.startswith("/ws/")):
                self._handle_ws(path, qs)
                return

            if path in ("/", "/index.html"):
                self._send_text(INDEX_HTML, "text/html; charset=utf-8")
                return

            if path.startswith("/static/"):
                self._serve_static(path)
                return

            if path.startswith("/api/"):
                self._handle_api(method, path, qs)
                return

            self.send_error(404, "Not found")
        except DockerError as e:
            self._send_error_json(e.message, 502 if e.status == 0 else e.status)
        except BrokenPipeError:
            pass
        except Exception as e:
            self._send_error_json(f"{type(e).__name__}: {e}", 500)

    def _handle_api(self, method, path, qs):
        parts = path.split("/")
        # ['', 'api', '<res>', '<sub>?', '<action>?']
        res = parts[2] if len(parts) > 2 else ""
        sub = parts[3] if len(parts) > 3 else ""
        action = parts[4] if len(parts) > 4 else ""

        # ---- system ----
        if res == "system" and method == "GET":
            try:
                ver = DOCKER.system_version()
            except DockerError as e:
                return self._send_json({"connected": False, "error": e.message}, 200)
            try:
                df = DOCKER.system_df()
            except DockerError:
                df = {}
            try:
                info = DOCKER.system_info()
            except DockerError:
                info = {}
            return self._send_json({
                "connected": True,
                "version": ver,
                "df": df,
                "info": {
                    "Name": info.get("Name"),
                    "OS": info.get("OperatingSystem"),
                    "KernelVersion": info.get("KernelVersion"),
                    "Arch": info.get("Architecture"),
                    "NCPU": info.get("NCPU"),
                    "MemTotal": info.get("MemTotal"),
                    "Containers": info.get("Containers"),
                    "ContainersRunning": info.get("ContainersRunning"),
                    "ContainersPaused": info.get("ContainersPaused"),
                    "ContainersStopped": info.get("ContainersStopped"),
                    "Images": info.get("Images"),
                    "ServerVersion": info.get("ServerVersion"),
                },
            })

        # ---- containers ----
        if res == "containers":
            if method == "GET" and not sub:
                return self._send_json(DOCKER.containers_list(all=True))
            if method == "POST" and sub == "prune":
                return self._send_json(DOCKER.containers_prune())
            if method == "POST" and sub == "run":
                return self._handle_run(self._read_body_json())
            if method == "POST" and sub and action in ("start", "stop", "restart", "kill", "pause", "unpause"):
                getattr(DOCKER, f"container_{action}")(sub)
                with self._tty_lock:
                    self._tty_cache.pop(sub, None)
                return self._send_json({"ok": True})
            if method == "POST" and sub and action == "rename":
                new = qs.get("name") or self._read_body_json().get("name")
                if not new:
                    return self._send_error_json("missing name", 400)
                DOCKER.container_rename(sub, new)
                return self._send_json({"ok": True})
            if method == "GET" and sub and action == "inspect":
                return self._send_json(DOCKER.container_inspect(sub))
            if method == "GET" and sub and action == "logs":
                return self._stream_logs(sub, tail=qs.get("tail", "500"))
            if method == "DELETE" and sub and not action:
                DOCKER.container_remove(sub, force=qs.get("force") == "1", volumes=qs.get("v") == "1")
                with self._tty_lock:
                    self._tty_cache.pop(sub, None)
                return self._send_json({"ok": True})

        # ---- images ----
        if res == "images":
            if method == "GET" and not sub:
                return self._send_json(DOCKER.images_list())
            if method == "POST" and sub == "pull":
                ref = qs.get("ref") or self._read_body_json().get("ref")
                if not ref:
                    return self._send_error_json("missing ref", 400)
                return self._stream_pull(ref)
            if method == "POST" and sub == "prune":
                return self._send_json(DOCKER.images_prune(dangling_only=qs.get("all") != "1"))
            if method == "GET" and sub and action == "inspect":
                return self._send_json(DOCKER.image_inspect(sub))
            if method == "DELETE" and sub and not action:
                return self._send_json(DOCKER.image_remove(sub, force=qs.get("force") == "1"))

        # ---- volumes ----
        if res == "volumes":
            if method == "GET" and not sub:
                return self._send_json(DOCKER.volumes_list())
            if method == "POST" and not sub:
                b = self._read_body_json()
                if not b.get("Name"):
                    return self._send_error_json("missing Name", 400)
                return self._send_json(DOCKER.volume_create(b["Name"], b.get("Driver", "local")))
            if method == "POST" and sub == "prune":
                return self._send_json(DOCKER.volumes_prune())
            if method == "GET" and sub and action == "inspect":
                return self._send_json(DOCKER.volume_inspect(sub))
            if method == "DELETE" and sub:
                DOCKER.volume_remove(sub, force=qs.get("force") == "1")
                return self._send_json({"ok": True})

        # ---- networks ----
        if res == "networks":
            if method == "GET" and not sub:
                return self._send_json(DOCKER.networks_list())
            if method == "POST" and not sub:
                b = self._read_body_json()
                if not b.get("Name"):
                    return self._send_error_json("missing Name", 400)
                return self._send_json(DOCKER.network_create(b["Name"], b.get("Driver", "bridge")))
            if method == "POST" and sub == "prune":
                return self._send_json(DOCKER.networks_prune())
            if method == "GET" and sub and action == "inspect":
                return self._send_json(DOCKER.network_inspect(sub))
            if method == "POST" and sub and action in ("connect", "disconnect"):
                b = self._read_body_json()
                cid = b.get("container") or b.get("Container")
                if not cid:
                    return self._send_error_json("missing container", 400)
                if action == "connect":
                    aliases = b.get("aliases") or None
                    DOCKER.network_connect(sub, cid, aliases=aliases)
                else:
                    DOCKER.network_disconnect(sub, cid, force=bool(b.get("force")))
                return self._send_json({"ok": True})
            if method == "DELETE" and sub:
                DOCKER.network_remove(sub)
                return self._send_json({"ok": True})

        return self._send_error_json("not found", 404)

    # ---- run image (create + start) ----

    def _handle_run(self, spec):
        image = spec.get("image")
        if not image:
            return self._send_error_json("missing image", 400)
        name = spec.get("name") or None

        body = {
            "Image": image,
            "Tty": bool(spec.get("tty", False)),
            "OpenStdin": bool(spec.get("stdin", False)),
            "AttachStdin": False, "AttachStdout": False, "AttachStderr": False,
            "Env": [],
            "ExposedPorts": {},
            "HostConfig": {},
        }
        if spec.get("cmd"):
            cmd = spec["cmd"]
            if isinstance(cmd, str):
                cmd = shlex.split(cmd)
            body["Cmd"] = cmd
        if spec.get("entrypoint"):
            ep = spec["entrypoint"]
            if isinstance(ep, str):
                ep = shlex.split(ep)
            body["Entrypoint"] = ep
        if spec.get("workdir"):
            body["WorkingDir"] = spec["workdir"]
        for kv in (spec.get("env") or []):
            kv = kv.strip()
            if kv:
                body["Env"].append(kv)

        port_bindings = {}
        for p in (spec.get("ports") or []):
            p = (p or "").strip()
            if not p:
                continue
            try:
                if ":" in p:
                    host, rest = p.split(":", 1)
                else:
                    host, rest = "", p
                if "/" in rest:
                    cport, proto = rest.split("/", 1)
                else:
                    cport, proto = rest, "tcp"
                key = f"{cport.strip()}/{proto.strip()}"
                body["ExposedPorts"][key] = {}
                port_bindings.setdefault(key, []).append({"HostPort": host.strip()})
            except ValueError:
                pass
        if port_bindings:
            body["HostConfig"]["PortBindings"] = port_bindings

        binds = [v.strip() for v in (spec.get("volumes") or []) if v and v.strip()]
        if binds:
            body["HostConfig"]["Binds"] = binds

        if spec.get("network"):
            body["HostConfig"]["NetworkMode"] = spec["network"]

        restart = spec.get("restart")
        if restart and restart != "no":
            body["HostConfig"]["RestartPolicy"] = {"Name": restart}

        if spec.get("autoremove"):
            body["HostConfig"]["AutoRemove"] = True

        created = DOCKER.container_create(name, body)
        cid = created.get("Id")
        if cid:
            DOCKER.container_start(cid)
        return self._send_json({"id": cid, "warnings": created.get("Warnings") or []})

    # ---- log streaming (SSE) ----

    def _container_uses_tty(self, cid):
        with self._tty_lock:
            if cid in self._tty_cache:
                return self._tty_cache[cid]
        try:
            info = DOCKER.container_inspect(cid)
            tty = bool(info.get("Config", {}).get("Tty"))
        except Exception:
            tty = False
        with self._tty_lock:
            self._tty_cache[cid] = tty
        return tty

    @staticmethod
    def _demux_log_step(buffer):
        """Consume complete 8-byte-header frames; return (text, leftover_bytes)."""
        out = []
        i = 0
        n = len(buffer)
        while i + 8 <= n:
            stream_type = buffer[i]
            if stream_type not in (0, 1, 2):
                out.append(buffer[i:].decode("utf-8", "replace"))
                return "".join(out), b""
            size = struct.unpack(">I", buffer[i + 4:i + 8])[0]
            if i + 8 + size > n:
                break
            out.append(buffer[i + 8:i + 8 + size].decode("utf-8", "replace"))
            i += 8 + size
        return "".join(out), buffer[i:]

    def _stream_logs(self, cid, tail="500"):
        self._send_sse_headers()
        tty = self._container_uses_tty(cid)
        try:
            buffer = b""
            for chunk in DOCKER.container_logs(cid, tail=tail):
                if tty:
                    text = chunk.decode("utf-8", "replace")
                else:
                    buffer += chunk
                    text, buffer = self._demux_log_step(buffer)
                if text and not self._sse_send(text):
                    return
        except DockerError as e:
            self._sse_send(f"[error] {e.message}", "error")
        except Exception as e:
            self._sse_send(f"[error] {e}", "error")
        finally:
            self._sse_send("", "done")

    def _stream_pull(self, ref):
        self._send_sse_headers()
        try:
            buf = b""
            for chunk in DOCKER.image_pull(ref):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if not self._sse_send(line.decode("utf-8", "replace")):
                        return
            if buf.strip():
                self._sse_send(buf.decode("utf-8", "replace"))
            self._sse_send("ok", "done")
        except DockerError as e:
            self._sse_send(json.dumps({"error": e.message}), "error")
        except Exception as e:
            self._sse_send(json.dumps({"error": str(e)}), "error")

    # ---- WebSocket: /ws/exec/<cid>?cmd=/bin/sh ----

    def _handle_ws(self, path, qs):
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_error(400, "missing Sec-WebSocket-Key")
            return

        parts = path.split("/")
        # ['', 'ws', 'exec', '<cid>']
        if len(parts) < 4 or parts[2] != "exec":
            self.send_error(404, "not found")
            return
        cid = parts[3]
        cmd = qs.get("cmd") or "/bin/sh"

        try:
            created = DOCKER.exec_create(cid, cmd, tty=True)
            exec_id = created.get("Id")
            if not exec_id:
                self.send_error(502, "exec create failed")
                return
            docker_sock, prefetched = DOCKER.exec_start_hijack(exec_id, tty=True)
        except DockerError as e:
            self.send_error(502, e.message)
            return

        try:
            self.wfile.write(ws_handshake_bytes(key))
            self.wfile.flush()
        except Exception:
            try:
                docker_sock.close()
            except Exception:
                pass
            return

        client_sock = self.connection
        send_lock = threading.Lock()
        stop = threading.Event()

        def safe_send(payload, opcode=OP_BIN):
            try:
                with send_lock:
                    ws_send_frame(client_sock, payload, opcode)
                return True
            except Exception:
                stop.set()
                return False

        if prefetched:
            safe_send(prefetched, OP_BIN)

        def docker_to_client():
            try:
                while not stop.is_set():
                    data = docker_sock.recv(4096)
                    if not data:
                        break
                    if not safe_send(data, OP_BIN):
                        break
            except Exception:
                pass
            finally:
                stop.set()
                # Tell client we're done
                try:
                    with send_lock:
                        ws_send_frame(client_sock, b"", OP_CLOSE)
                except Exception:
                    pass

        def client_to_docker():
            try:
                while not stop.is_set():
                    frame = ws_recv_frame(client_sock)
                    if frame is None:
                        break
                    op, data = frame
                    if op == OP_CLOSE:
                        break
                    if op == OP_PING:
                        safe_send(data, OP_PONG)
                        continue
                    if op == OP_PONG:
                        continue
                    if op == OP_TEXT:
                        try:
                            obj = json.loads(data.decode("utf-8"))
                        except Exception:
                            obj = None
                        if isinstance(obj, dict) and obj.get("type") == "resize":
                            try:
                                DOCKER.exec_resize(exec_id, int(obj.get("cols", 80)), int(obj.get("rows", 24)))
                            except Exception:
                                pass
                            continue
                        try:
                            docker_sock.sendall(data)
                        except Exception:
                            break
                    elif op == OP_BIN:
                        try:
                            docker_sock.sendall(data)
                        except Exception:
                            break
            except Exception:
                pass
            finally:
                stop.set()

        t1 = threading.Thread(target=docker_to_client, daemon=True)
        t2 = threading.Thread(target=client_to_docker, daemon=True)
        t1.start()
        t2.start()
        stop.wait()
        try:
            docker_sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            docker_sock.close()
        except Exception:
            pass
        # Mark connection to be closed by handler
        self.close_connection = True


# =============================================================================
# Embedded frontend
# =============================================================================

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<link rel="icon" type="image/png" href="/static/64538097.png"/>
<title>Talentia Docker Viewer</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"/>
<style>
  :root, :root[data-theme="dark"]{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c232c; --border:#30363d;
    --text:#e6edf3; --muted:#8b949e; --accent:#2f81f7; --good:#3fb950;
    --warn:#d29922; --bad:#f85149; --chip:#21262d;
  }
  :root[data-theme="light"]{
    --bg:#ffffff; --panel:#f6f8fa; --panel2:#eaeef2; --border:#d0d7de;
    --text:#1f2328; --muted:#656d76; --accent:#0969da; --good:#1a7f37;
    --warn:#9a6700; --bad:#cf222e; --chip:#eaeef2;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
    font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  a{color:var(--accent);text-decoration:none}
  button,input,select,textarea{font:inherit;color:inherit}
  code,kbd,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
  /* Layout */
  .app{display:grid;grid-template-rows:48px 40px 1fr 26px;height:100vh}
  header{display:flex;align-items:center;gap:14px;padding:0 16px;background:var(--panel);
    border-bottom:1px solid var(--border)}
  header .logo{font-weight:600;letter-spacing:.5px}
  header .logo .dot{display:inline-block;width:8px;height:8px;border-radius:50%;
    background:var(--good);margin-right:8px;vertical-align:middle}
  header .logo.off .dot{background:var(--bad)}
  header .spacer{flex:1}
  header .status{color:var(--muted);font-size:12px}
  nav.tabs{display:flex;gap:0;padding:0 12px;background:var(--panel);
    border-bottom:1px solid var(--border)}
  nav.tabs button{background:none;border:0;padding:0 16px;height:40px;color:var(--muted);
    cursor:pointer;border-bottom:2px solid transparent}
  nav.tabs button.active{color:var(--text);border-bottom-color:var(--accent)}
  main{overflow:auto;padding:14px 16px}
  footer{display:flex;align-items:center;gap:14px;padding:0 16px;background:var(--panel);
    border-top:1px solid var(--border);color:var(--muted);font-size:11px}
  /* Bars */
  .bar{display:flex;align-items:center;gap:8px;margin-bottom:10px}
  .bar .grow{flex:1}
  .bar input[type=search]{background:var(--panel);border:1px solid var(--border);
    border-radius:6px;padding:6px 10px;min-width:240px;color:var(--text)}
  .btn{background:var(--panel);border:1px solid var(--border);border-radius:6px;
    padding:6px 12px;color:var(--text);cursor:pointer}
  .btn:hover{border-color:#444c56}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .btn.primary:hover{filter:brightness(1.1)}
  .btn.danger{color:var(--bad)}
  .btn.danger:hover{background:rgba(248,81,73,.1);border-color:var(--bad)}
  .btn.icon{padding:4px 8px;font-size:12px}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  /* Tables */
  table.t{width:100%;border-collapse:collapse;background:var(--panel);
    border:1px solid var(--border);border-radius:8px;overflow:hidden}
  table.t thead{background:var(--panel2)}
  table.t th,table.t td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border);
    vertical-align:middle}
  table.t tbody tr:last-child td{border-bottom:0}
  table.t tbody tr:hover{background:rgba(255,255,255,.02)}
  table.t th{font-weight:600;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  table.t .id{font-family:ui-monospace,Menlo,Consolas,monospace;color:var(--muted);font-size:11px}
  table.t .actions{text-align:right;white-space:nowrap}
  table.t .actions .btn.icon{margin-left:4px}
  /* Status pill */
  .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;
    background:var(--chip);color:var(--muted)}
  .pill.running{background:rgba(63,185,80,.15);color:var(--good)}
  .pill.exited,.pill.dead{background:rgba(248,81,73,.15);color:var(--bad)}
  .pill.paused,.pill.restarting{background:rgba(210,153,34,.15);color:var(--warn)}
  .pill.created{background:rgba(47,129,247,.15);color:var(--accent)}
  /* Stats cards */
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:14px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px}
  .card .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  .card .v{font-size:20px;margin-top:4px;font-weight:600}
  /* Toast */
  #toasts{position:fixed;right:16px;bottom:32px;display:flex;flex-direction:column;gap:8px;z-index:1000}
  .toast{background:var(--panel);border:1px solid var(--border);border-left-width:3px;
    padding:8px 14px;border-radius:6px;min-width:260px;max-width:420px;
    box-shadow:0 4px 12px rgba(0,0,0,.5)}
  .toast.ok{border-left-color:var(--good)}
  .toast.err{border-left-color:var(--bad)}
  .toast.info{border-left-color:var(--accent)}
  /* Modals */
  dialog{background:var(--panel);color:var(--text);border:1px solid var(--border);
    border-radius:10px;padding:0;max-width:90vw;max-height:90vh;width:560px;
    box-shadow:0 10px 40px rgba(0,0,0,.5)}
  dialog::backdrop{background:rgba(0,0,0,.6)}
  dialog .head{display:flex;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border)}
  dialog .head h3{margin:0;flex:1;font-size:14px;font-weight:600}
  dialog .head .close{background:none;border:0;color:var(--muted);font-size:20px;cursor:pointer;padding:0 6px}
  dialog .body{padding:14px 16px;overflow:auto;max-height:70vh}
  dialog .foot{padding:10px 16px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px}
  dialog.wide{width:880px}
  dialog.tall .body{max-height:75vh}
  /* Forms */
  .form .row{display:grid;grid-template-columns:140px 1fr;gap:10px;margin-bottom:10px;align-items:center}
  .form label{color:var(--muted);font-size:12px}
  .form input,.form select,.form textarea{width:100%;background:var(--bg);border:1px solid var(--border);
    border-radius:6px;padding:6px 10px;color:var(--text)}
  .form textarea{min-height:60px;resize:vertical;font-family:ui-monospace,Menlo,Consolas,monospace}
  .form .list-row{display:flex;gap:6px;margin-bottom:4px}
  .form .list-row input{flex:1}
  .form .list-row button{padding:4px 10px}
  /* Logs */
  .logbox{background:#000;color:#cfd2d5;font-family:ui-monospace,Menlo,Consolas,monospace;
    font-size:12px;padding:10px;height:55vh;overflow:auto;white-space:pre-wrap;word-break:break-all;
    border-radius:6px;border:1px solid var(--border)}
  .pullbox{background:#000;color:#cfd2d5;font-family:ui-monospace,Menlo,Consolas,monospace;
    font-size:12px;padding:10px;height:260px;overflow:auto;white-space:pre-wrap;border-radius:6px;
    border:1px solid var(--border)}
  pre.inspect{background:#000;color:#cfd2d5;padding:10px;border-radius:6px;border:1px solid var(--border);
    max-height:60vh;overflow:auto;font-size:12px;white-space:pre-wrap}
  /* xterm container */
  #termhost{width:100%;height:60vh;background:#000;padding:6px;border-radius:6px;border:1px solid var(--border)}
  .empty{padding:30px;text-align:center;color:var(--muted)}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="logo" id="brand"><span class="dot"></span>Talentia Docker Viewer</div>
    <span class="status" id="dockerVer">connecting...</span>
    <div class="spacer"></div>
    <button class="btn icon" id="themeBtn" title="Toggle light/dark theme">Light</button>
    <button class="btn icon" id="refreshBtn" title="Refresh">Refresh</button>
  </header>
  <nav class="tabs" id="tabs">
    <button data-tab="containers" class="active">Containers</button>
    <button data-tab="images">Images</button>
    <button data-tab="volumes">Volumes</button>
    <button data-tab="networks">Networks</button>
    <button data-tab="system">System</button>
  </nav>
  <main id="main"></main>
  <footer>
    <span id="footHost">host: -</span>
    <span>·</span>
    <span id="footCounts">- containers · - images</span>
    <div class="spacer" style="flex:1"></div>
    <span id="footDisk">disk: -</span>
    <span>·</span>
    <span>Powered by Talentia Software</span>
  </footer>
</div>

<div id="toasts"></div>

<!-- Run image modal -->
<dialog id="runDlg" class="wide">
  <div class="head">
    <h3>Run a container</h3>
    <button class="close" data-close="runDlg">×</button>
  </div>
  <div class="body form">
    <div class="row"><label>Image</label><input id="r_image" placeholder="e.g. alpine:latest"/></div>
    <div class="row"><label>Name</label><input id="r_name" placeholder="(optional)"/></div>
    <div class="row"><label>Command</label><input id="r_cmd" placeholder="e.g. sh -c 'sleep 3600'"/></div>
    <div class="row"><label>Entrypoint</label><input id="r_entrypoint" placeholder="(optional override)"/></div>
    <div class="row"><label>Working dir</label><input id="r_workdir" placeholder="(optional)"/></div>
    <div class="row"><label>Ports</label>
      <div id="r_ports"></div></div>
    <div class="row"><label></label><button class="btn icon" id="r_addPort">+ Add port</button></div>
    <div class="row"><label>Environment</label>
      <div id="r_envs"></div></div>
    <div class="row"><label></label><button class="btn icon" id="r_addEnv">+ Add env</button></div>
    <div class="row"><label>Volumes</label>
      <div id="r_vols"></div></div>
    <div class="row"><label></label><button class="btn icon" id="r_addVol">+ Add volume</button></div>
    <div class="row"><label>Network</label><input id="r_network" placeholder="(optional, e.g. host, bridge, my-net)"/></div>
    <div class="row"><label>Restart policy</label>
      <select id="r_restart">
        <option value="no">no</option>
        <option value="on-failure">on-failure</option>
        <option value="always">always</option>
        <option value="unless-stopped">unless-stopped</option>
      </select>
    </div>
    <div class="row"><label>Options</label>
      <div>
        <label style="display:inline-flex;align-items:center;gap:6px;color:var(--text)">
          <input type="checkbox" id="r_tty"/> TTY</label>
        <label style="display:inline-flex;align-items:center;gap:6px;color:var(--text);margin-left:14px">
          <input type="checkbox" id="r_stdin"/> Interactive (stdin)</label>
        <label style="display:inline-flex;align-items:center;gap:6px;color:var(--text);margin-left:14px">
          <input type="checkbox" id="r_autoremove"/> Auto-remove on exit</label>
      </div>
    </div>
  </div>
  <div class="foot">
    <button class="btn" data-close="runDlg">Cancel</button>
    <button class="btn primary" id="runGo">Run</button>
  </div>
</dialog>

<!-- Pull modal -->
<dialog id="pullDlg">
  <div class="head">
    <h3>Pull image</h3>
    <button class="close" data-close="pullDlg">×</button>
  </div>
  <div class="body form">
    <div class="row"><label>Reference</label><input id="p_ref" placeholder="e.g. nginx:alpine"/></div>
    <div class="pullbox" id="p_log" style="margin-top:8px"></div>
  </div>
  <div class="foot">
    <button class="btn" data-close="pullDlg">Close</button>
    <button class="btn primary" id="pullGo">Pull</button>
  </div>
</dialog>

<!-- Logs modal -->
<dialog id="logsDlg" class="wide tall">
  <div class="head">
    <h3 id="logsTitle">Logs</h3>
    <button class="close" data-close="logsDlg">×</button>
  </div>
  <div class="body">
    <div class="bar">
      <label style="color:var(--muted);font-size:12px"><input type="checkbox" id="logAuto" checked/> Auto-scroll</label>
      <button class="btn icon" id="logClear">Clear</button>
    </div>
    <div class="logbox" id="logBox"></div>
  </div>
</dialog>

<!-- Inspect modal -->
<dialog id="inspectDlg" class="wide tall">
  <div class="head">
    <h3 id="inspectTitle">Inspect</h3>
    <button class="close" data-close="inspectDlg">×</button>
  </div>
  <div class="body"><pre class="inspect" id="inspectBody"></pre></div>
</dialog>

<!-- Exec terminal modal -->
<dialog id="termDlg" class="wide tall">
  <div class="head">
    <h3 id="termTitle">Terminal</h3>
    <button class="close" data-close="termDlg">×</button>
  </div>
  <div class="body">
    <div class="bar">
      <label style="color:var(--muted);font-size:12px">Command:</label>
      <input id="termCmd" value="/bin/sh" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:4px 8px;width:240px"/>
      <button class="btn icon" id="termRestart">Restart</button>
      <span class="grow"></span>
      <span class="status" id="termStatus" style="color:var(--muted);font-size:12px">disconnected</span>
    </div>
    <div id="termhost"></div>
  </div>
</dialog>

<!-- Network manage (attach/detach containers) -->
<dialog id="netDlg" class="wide tall">
  <div class="head">
    <h3 id="netTitle">Network</h3>
    <button class="close" data-close="netDlg">×</button>
  </div>
  <div class="body">
    <p style="color:var(--muted);margin:0 0 10px">
      Check a container to attach it to this network. Uncheck to detach.
      Containers on the same network can reach each other by container name (DNS).
    </p>
    <table class="t">
      <thead><tr>
        <th style="width:50px"></th>
        <th>Container</th><th>Image</th><th>State</th><th>Aliases</th>
      </tr></thead>
      <tbody id="netList"></tbody>
    </table>
  </div>
  <div class="foot">
    <button class="btn primary" data-close="netDlg">Done</button>
  </div>
</dialog>

<!-- Create volume / network -->
<dialog id="createDlg">
  <div class="head">
    <h3 id="createTitle">Create</h3>
    <button class="close" data-close="createDlg">×</button>
  </div>
  <div class="body form">
    <div class="row"><label>Name</label><input id="c_name"/></div>
    <div class="row"><label>Driver</label><input id="c_driver"/></div>
  </div>
  <div class="foot">
    <button class="btn" data-close="createDlg">Cancel</button>
    <button class="btn primary" id="createGo">Create</button>
  </div>
</dialog>

<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script>
'use strict';

/* ========== utils ========== */
const $ = sel => document.querySelector(sel);
const $$ = sel => Array.from(document.querySelectorAll(sel));
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtBytes = n => {
  if (n == null || isNaN(n)) return '-';
  const u = ['B','KB','MB','GB','TB']; let i = 0; n = +n;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n < 10 && i > 0 ? 1 : 0) + ' ' + u[i];
};
const shortId = id => (id || '').replace(/^sha256:/, '').slice(0, 12);
const ago = ts => {
  if (!ts) return '-';
  const d = Math.floor(Date.now()/1000 - ts);
  if (d < 60) return d + 's ago';
  if (d < 3600) return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  return Math.floor(d/86400) + 'd ago';
};
async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {'Content-Type':'application/json'}}, opts || {}));
  let body = null;
  try { body = await r.json(); } catch (_) {}
  if (!r.ok) throw new Error((body && body.error) || ('HTTP ' + r.status));
  return body;
}
function toast(msg, type='info', ms=3500) {
  const el = document.createElement('div');
  el.className = 'toast ' + (type === 'err' ? 'err' : type === 'ok' ? 'ok' : 'info');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), ms);
}
function confirmDlg(msg) { return Promise.resolve(window.confirm(msg)); }
function openDlg(id) { const d = document.getElementById(id); if (d && !d.open) d.showModal(); }
function closeDlg(id) { const d = document.getElementById(id); if (d && d.open) d.close(); }
$$('[data-close]').forEach(b => b.addEventListener('click', e => closeDlg(b.dataset.close)));

/* ========== app state ========== */
const state = {
  tab: 'containers',
  filter: '',
  pollTimer: null,
};

/* ========== tab switching ========== */
function setTab(t) {
  state.tab = t;
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  state.filter = '';
  render();
  scheduleRefresh();
}
$$('#tabs button').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
$('#refreshBtn').addEventListener('click', () => refresh());

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('taldocker_theme', t); } catch(_){}
  const b = document.getElementById('themeBtn');
  if (b) b.textContent = (t === 'light') ? 'Dark' : 'Light';
}
$('#themeBtn').addEventListener('click', () => {
  const cur = document.documentElement.dataset.theme || 'dark';
  applyTheme(cur === 'light' ? 'dark' : 'light');
});
(function bootTheme(){
  let saved = 'dark';
  try { saved = localStorage.getItem('taldocker_theme') || 'dark'; } catch(_){}
  applyTheme(saved);
})();

function scheduleRefresh() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(refresh, 2500);
}
async function refresh() {
  try { await renderTab(state.tab); }
  catch (e) { /* silent */ }
  refreshFooter();
}

/* ========== render dispatcher ========== */
function render() { renderTab(state.tab).catch(e => toast(e.message, 'err')); }

async function renderTab(t) {
  if (t === 'containers') return renderContainers();
  if (t === 'images') return renderImages();
  if (t === 'volumes') return renderVolumes();
  if (t === 'networks') return renderNetworks();
  if (t === 'system') return renderSystem();
}

/* ========== Containers ========== */
async function renderContainers() {
  const list = await api('/api/containers');
  const filter = state.filter.toLowerCase();
  const rows = list.filter(c => {
    if (!filter) return true;
    const name = (c.Names || []).join(',');
    return (name + c.Image + (c.Id || '')).toLowerCase().includes(filter);
  });
  const html = `
    <div class="bar">
      <input type="search" id="cFilter" placeholder="Filter by name / image / id..." value="${esc(state.filter)}"/>
      <span class="grow"></span>
      <button class="btn" id="cPrune">Prune stopped</button>
      <button class="btn primary" id="cRun">+ Run image</button>
    </div>
    <table class="t">
      <thead><tr>
        <th style="width:60px"></th>
        <th>Name</th><th>Image</th><th>Status</th><th>Ports</th><th>Created</th>
        <th class="actions">Actions</th>
      </tr></thead>
      <tbody>
        ${rows.length === 0 ? `<tr><td colspan="7" class="empty">No containers</td></tr>` : rows.map(rowContainer).join('')}
      </tbody>
    </table>`;
  $('#main').innerHTML = html;
  $('#cFilter').addEventListener('input', e => { state.filter = e.target.value; renderContainers(); });
  $('#cRun').addEventListener('click', () => openRunDialog());
  $('#cPrune').addEventListener('click', async () => {
    if (!await confirmDlg('Prune all stopped containers?')) return;
    try { const r = await api('/api/containers/prune', {method:'POST'}); toast(`Pruned ${(r.ContainersDeleted||[]).length} container(s)`, 'ok'); refresh(); }
    catch (e) { toast(e.message, 'err'); }
  });
  bindContainerActions();
}
function rowContainer(c) {
  const name = (c.Names || ['/?'])[0].replace(/^\//, '');
  const stateName = (c.State || '').toLowerCase();
  const portsHtml = (c.Ports || []).filter(p => p.PublicPort).map(p => {
    const label = `${p.IP || '0.0.0.0'}:${p.PublicPort}->${p.PrivatePort}/${p.Type}`;
    if (p.Type === 'tcp') {
      return `<a href="http://${location.hostname}:${p.PublicPort}" target="_blank" rel="noopener" title="Open http://${location.hostname}:${p.PublicPort} in a new tab">${esc(label)}</a>`;
    }
    return esc(label);
  }).join(', ');
  const running = stateName === 'running';
  const id = c.Id;
  return `<tr data-id="${esc(id)}">
    <td><span class="id">${esc(shortId(id))}</span></td>
    <td>${esc(name)}</td>
    <td>${esc(c.Image)}</td>
    <td><span class="pill ${stateName}">${esc(stateName)}</span> <span class="id">${esc(c.Status||'')}</span></td>
    <td class="id">${portsHtml || '-'}</td>
    <td class="id">${esc(ago(c.Created))}</td>
    <td class="actions">
      ${running
        ? `<button class="btn icon" data-act="stop" title="Stop">Stop</button>
           <button class="btn icon" data-act="restart" title="Restart">Restart</button>
           <button class="btn icon" data-act="exec" title="Open terminal">Terminal</button>`
        : `<button class="btn icon" data-act="start" title="Start">Start</button>`}
      <button class="btn icon" data-act="logs" title="Logs">Logs</button>
      <button class="btn icon" data-act="inspect" title="Inspect">Inspect</button>
      <button class="btn icon danger" data-act="remove" title="Remove">Remove</button>
    </td>
  </tr>`;
}
function bindContainerActions() {
  $$('#main tbody [data-act]').forEach(b => {
    b.addEventListener('click', async ev => {
      const tr = b.closest('tr');
      const id = tr.dataset.id;
      const act = b.dataset.act;
      try {
        if (act === 'start' || act === 'stop' || act === 'restart') {
          await api(`/api/containers/${id}/${act}`, {method:'POST'});
          toast(`Container ${act}ed`, 'ok'); refresh();
        } else if (act === 'remove') {
          const force = !(await confirmDlg('Remove this container? Click Cancel to force-remove (also if running).'));
          await api(`/api/containers/${id}?force=${force?'1':'0'}`, {method:'DELETE'});
          toast('Container removed', 'ok'); refresh();
        } else if (act === 'logs') {
          openLogs(id, (tr.children[1].textContent||'').trim());
        } else if (act === 'inspect') {
          openInspect('container', id, tr.children[1].textContent.trim());
        } else if (act === 'exec') {
          openTerminal(id, (tr.children[1].textContent||'').trim());
        }
      } catch (e) { toast(e.message, 'err'); }
    });
  });
}

/* ========== Images ========== */
async function renderImages() {
  const list = await api('/api/images');
  const filter = state.filter.toLowerCase();
  const rows = list.filter(im => {
    if (!filter) return true;
    return ((im.RepoTags||[]).join(',') + (im.Id||'')).toLowerCase().includes(filter);
  });
  $('#main').innerHTML = `
    <div class="bar">
      <input type="search" id="iFilter" placeholder="Filter images..." value="${esc(state.filter)}"/>
      <span class="grow"></span>
      <button class="btn" id="iPrune">Prune dangling</button>
      <button class="btn" id="iPull">+ Pull</button>
      <button class="btn primary" id="iRun">Run...</button>
    </div>
    <table class="t">
      <thead><tr>
        <th style="width:60px"></th>
        <th>Repository : Tag</th><th>Size</th><th>Created</th>
        <th class="actions">Actions</th>
      </tr></thead>
      <tbody>
        ${rows.length === 0 ? `<tr><td colspan="5" class="empty">No images</td></tr>` : rows.map(rowImage).join('')}
      </tbody>
    </table>`;
  $('#iFilter').addEventListener('input', e => { state.filter = e.target.value; renderImages(); });
  $('#iPull').addEventListener('click', () => openPullDialog());
  $('#iRun').addEventListener('click', () => openRunDialog());
  $('#iPrune').addEventListener('click', async () => {
    if (!await confirmDlg('Prune dangling images?')) return;
    try { const r = await api('/api/images/prune', {method:'POST'}); toast(`Pruned, freed ${fmtBytes(r.SpaceReclaimed||0)}`, 'ok'); refresh(); }
    catch (e) { toast(e.message, 'err'); }
  });
  $$('#main tbody [data-act]').forEach(b => {
    b.addEventListener('click', async () => {
      const tr = b.closest('tr');
      const id = tr.dataset.id;
      const ref = tr.dataset.ref || id;
      const act = b.dataset.act;
      try {
        if (act === 'remove') {
          const force = !(await confirmDlg('Remove this image? Click Cancel to force-remove.'));
          await api(`/api/images/${encodeURIComponent(ref)}?force=${force?'1':'0'}`, {method:'DELETE'});
          toast('Image removed', 'ok'); refresh();
        } else if (act === 'run') {
          openRunDialog(ref);
        } else if (act === 'inspect') {
          openInspect('image', ref, ref);
        }
      } catch (e) { toast(e.message, 'err'); }
    });
  });
}
function rowImage(im) {
  const tags = (im.RepoTags && im.RepoTags.length) ? im.RepoTags : ['<none>:<none>'];
  const primary = tags[0];
  return `<tr data-id="${esc(im.Id)}" data-ref="${esc(primary === '<none>:<none>' ? im.Id : primary)}">
    <td><span class="id">${esc(shortId(im.Id))}</span></td>
    <td>${tags.map(t => `<div>${esc(t)}</div>`).join('')}</td>
    <td>${esc(fmtBytes(im.Size))}</td>
    <td class="id">${esc(ago(im.Created))}</td>
    <td class="actions">
      <button class="btn icon" data-act="run" title="Run">Run</button>
      <button class="btn icon" data-act="inspect">Inspect</button>
      <button class="btn icon danger" data-act="remove">Remove</button>
    </td>
  </tr>`;
}

/* ========== Volumes ========== */
async function renderVolumes() {
  const data = await api('/api/volumes');
  const list = (data && data.Volumes) || [];
  const filter = state.filter.toLowerCase();
  const rows = list.filter(v => !filter || (v.Name + v.Driver).toLowerCase().includes(filter));
  $('#main').innerHTML = `
    <div class="bar">
      <input type="search" id="vFilter" placeholder="Filter volumes..." value="${esc(state.filter)}"/>
      <span class="grow"></span>
      <button class="btn" id="vPrune">Prune unused</button>
      <button class="btn primary" id="vNew">+ Create</button>
    </div>
    <table class="t">
      <thead><tr><th>Name</th><th>Driver</th><th>Mountpoint</th><th>Created</th><th class="actions">Actions</th></tr></thead>
      <tbody>${rows.length === 0 ? `<tr><td colspan="5" class="empty">No volumes</td></tr>` : rows.map(rowVolume).join('')}</tbody>
    </table>`;
  $('#vFilter').addEventListener('input', e => { state.filter = e.target.value; renderVolumes(); });
  $('#vNew').addEventListener('click', () => openCreateDlg('volume'));
  $('#vPrune').addEventListener('click', async () => {
    if (!await confirmDlg('Prune unused volumes?')) return;
    try { const r = await api('/api/volumes/prune', {method:'POST'}); toast(`Pruned ${(r.VolumesDeleted||[]).length} volume(s)`, 'ok'); refresh(); }
    catch (e) { toast(e.message, 'err'); }
  });
  $$('#main tbody [data-act]').forEach(b => {
    b.addEventListener('click', async () => {
      const tr = b.closest('tr');
      const name = tr.dataset.name;
      const act = b.dataset.act;
      try {
        if (act === 'remove') {
          if (!await confirmDlg(`Remove volume "${name}"?`)) return;
          await api(`/api/volumes/${encodeURIComponent(name)}`, {method:'DELETE'});
          toast('Volume removed', 'ok'); refresh();
        } else if (act === 'inspect') {
          openInspect('volume', name, name);
        }
      } catch (e) { toast(e.message, 'err'); }
    });
  });
}
function rowVolume(v) {
  return `<tr data-name="${esc(v.Name)}">
    <td>${esc(v.Name)}</td>
    <td>${esc(v.Driver||'')}</td>
    <td class="id">${esc(v.Mountpoint||'')}</td>
    <td class="id">${esc(v.CreatedAt||'')}</td>
    <td class="actions">
      <button class="btn icon" data-act="inspect">Inspect</button>
      <button class="btn icon danger" data-act="remove">Remove</button>
    </td>
  </tr>`;
}

/* ========== Networks ========== */
async function renderNetworks() {
  const list = await api('/api/networks');
  const filter = state.filter.toLowerCase();
  const rows = list.filter(n => !filter || (n.Name + n.Driver + n.Id).toLowerCase().includes(filter));
  $('#main').innerHTML = `
    <div class="bar">
      <input type="search" id="nFilter" placeholder="Filter networks..." value="${esc(state.filter)}"/>
      <span class="grow"></span>
      <button class="btn" id="nPrune">Prune unused</button>
      <button class="btn primary" id="nNew">+ Create</button>
    </div>
    <table class="t">
      <thead><tr><th>Name</th><th>Driver</th><th>Scope</th><th>Subnet</th><th class="actions">Actions</th></tr></thead>
      <tbody>${rows.length === 0 ? `<tr><td colspan="5" class="empty">No networks</td></tr>` : rows.map(rowNetwork).join('')}</tbody>
    </table>`;
  $('#nFilter').addEventListener('input', e => { state.filter = e.target.value; renderNetworks(); });
  $('#nNew').addEventListener('click', () => openCreateDlg('network'));
  $('#nPrune').addEventListener('click', async () => {
    if (!await confirmDlg('Prune unused networks?')) return;
    try { const r = await api('/api/networks/prune', {method:'POST'}); toast(`Pruned ${(r.NetworksDeleted||[]).length} network(s)`, 'ok'); refresh(); }
    catch (e) { toast(e.message, 'err'); }
  });
  $$('#main tbody [data-act]').forEach(b => {
    b.addEventListener('click', async () => {
      const tr = b.closest('tr');
      const id = tr.dataset.id;
      const name = tr.dataset.name;
      const act = b.dataset.act;
      try {
        if (act === 'remove') {
          if (!await confirmDlg(`Remove network "${name}"?`)) return;
          await api(`/api/networks/${encodeURIComponent(id)}`, {method:'DELETE'});
          toast('Network removed', 'ok'); refresh();
        } else if (act === 'inspect') {
          openInspect('network', id, name);
        } else if (act === 'manage') {
          openNetworkManage(id, name);
        }
      } catch (e) { toast(e.message, 'err'); }
    });
  });
}
function rowNetwork(n) {
  const subnet = (n.IPAM && n.IPAM.Config || []).map(c => c.Subnet).filter(Boolean).join(', ');
  return `<tr data-id="${esc(n.Id)}" data-name="${esc(n.Name)}">
    <td>${esc(n.Name)}</td>
    <td>${esc(n.Driver||'')}</td>
    <td>${esc(n.Scope||'')}</td>
    <td class="id">${esc(subnet||'-')}</td>
    <td class="actions">
      <button class="btn icon" data-act="manage" title="Attach/detach containers">Manage</button>
      <button class="btn icon" data-act="inspect">Inspect</button>
      <button class="btn icon danger" data-act="remove">Remove</button>
    </td>
  </tr>`;
}

/* ========== System ========== */
async function renderSystem() {
  let s = {};
  try { s = await api('/api/system'); } catch (e) { $('#main').innerHTML = `<div class="empty">Cannot reach Docker: ${esc(e.message)}</div>`; return; }
  if (!s.connected) {
    $('#main').innerHTML = `<div class="empty" style="color:var(--bad)">Not connected: ${esc(s.error||'')}</div>`;
    return;
  }
  const v = s.version || {}; const i = s.info || {}; const df = s.df || {};
  const imgSize = (df.Images || []).reduce((a,b) => a + (b.Size || 0), 0);
  const ctnSize = (df.Containers || []).reduce((a,b) => a + (b.SizeRw || 0), 0);
  const volSize = (df.Volumes || []).reduce((a,b) => a + ((b.UsageData && b.UsageData.Size) || 0), 0);
  $('#main').innerHTML = `
    <div class="cards">
      <div class="card"><div class="k">Docker</div><div class="v">${esc(v.Version||'?')}</div></div>
      <div class="card"><div class="k">API</div><div class="v">${esc(v.ApiVersion||'?')}</div></div>
      <div class="card"><div class="k">Host</div><div class="v">${esc(i.Name||'?')}</div></div>
      <div class="card"><div class="k">OS / Kernel</div><div class="v" style="font-size:14px">${esc(i.OS||'?')}<br/><span style="color:var(--muted);font-size:11px">${esc(i.KernelVersion||'')}</span></div></div>
      <div class="card"><div class="k">CPUs</div><div class="v">${esc(i.NCPU||'?')}</div></div>
      <div class="card"><div class="k">Memory</div><div class="v">${fmtBytes(i.MemTotal)}</div></div>
      <div class="card"><div class="k">Containers</div><div class="v">${esc(i.ContainersRunning||0)} / ${esc(i.Containers||0)}</div></div>
      <div class="card"><div class="k">Images</div><div class="v">${esc(i.Images||0)}</div></div>
      <div class="card"><div class="k">Disk: images</div><div class="v">${fmtBytes(imgSize)}</div></div>
      <div class="card"><div class="k">Disk: containers</div><div class="v">${fmtBytes(ctnSize)}</div></div>
      <div class="card"><div class="k">Disk: volumes</div><div class="v">${fmtBytes(volSize)}</div></div>
    </div>
    <div class="bar">
      <button class="btn danger" id="sysPruneAll">Prune ALL (stopped containers + dangling images + unused volumes + networks)</button>
    </div>`;
  $('#sysPruneAll').addEventListener('click', async () => {
    if (!await confirmDlg('Remove ALL stopped containers, dangling images, unused volumes and networks? This cannot be undone.')) return;
    try {
      await api('/api/containers/prune', {method:'POST'});
      await api('/api/images/prune', {method:'POST'});
      await api('/api/volumes/prune', {method:'POST'});
      await api('/api/networks/prune', {method:'POST'});
      toast('Pruned everything', 'ok');
      refresh();
    } catch (e) { toast(e.message, 'err'); }
  });
}

/* ========== footer / status ========== */
async function refreshFooter() {
  try {
    const s = await api('/api/system');
    if (!s.connected) {
      $('#brand').classList.add('off');
      $('#dockerVer').textContent = 'disconnected';
      $('#footHost').textContent = 'host: -';
      $('#footCounts').textContent = '-';
      $('#footDisk').textContent = 'disk: -';
      return;
    }
    $('#brand').classList.remove('off');
    $('#dockerVer').textContent = `Docker ${s.version.Version} (API ${s.version.ApiVersion})`;
    $('#footHost').textContent = 'host: ' + (s.info.Name || '?');
    $('#footCounts').textContent = `${s.info.ContainersRunning||0}/${s.info.Containers||0} containers · ${s.info.Images||0} images`;
    const total = ((s.df.Images||[]).reduce((a,b) => a + (b.Size||0), 0))
                + ((s.df.Containers||[]).reduce((a,b) => a + (b.SizeRw||0), 0))
                + ((s.df.Volumes||[]).reduce((a,b) => a + ((b.UsageData && b.UsageData.Size)||0), 0));
    $('#footDisk').textContent = 'disk: ' + fmtBytes(total);
  } catch (e) {
    $('#brand').classList.add('off');
    $('#dockerVer').textContent = 'disconnected';
  }
}

/* ========== Run dialog ========== */
function addInputRow(container, val='') {
  const div = document.createElement('div');
  div.className = 'list-row';
  div.innerHTML = `<input value="${esc(val)}"/><button class="btn" type="button">×</button>`;
  div.querySelector('button').addEventListener('click', () => div.remove());
  container.appendChild(div);
}
function readInputRows(container) {
  return Array.from(container.querySelectorAll('input')).map(i => i.value);
}
function openRunDialog(image = '') {
  $('#r_image').value = image;
  $('#r_name').value = '';
  $('#r_cmd').value = '';
  $('#r_entrypoint').value = '';
  $('#r_workdir').value = '';
  $('#r_network').value = '';
  $('#r_restart').value = 'no';
  $('#r_tty').checked = false;
  $('#r_stdin').checked = false;
  $('#r_autoremove').checked = false;
  $('#r_ports').innerHTML = '';
  $('#r_envs').innerHTML = '';
  $('#r_vols').innerHTML = '';
  addInputRow($('#r_ports'), '');
  addInputRow($('#r_envs'), '');
  addInputRow($('#r_vols'), '');
  openDlg('runDlg');
}
$('#r_addPort').addEventListener('click', e => { e.preventDefault(); addInputRow($('#r_ports')); });
$('#r_addEnv').addEventListener('click', e => { e.preventDefault(); addInputRow($('#r_envs')); });
$('#r_addVol').addEventListener('click', e => { e.preventDefault(); addInputRow($('#r_vols')); });
$('#runGo').addEventListener('click', async () => {
  const spec = {
    image: $('#r_image').value.trim(),
    name: $('#r_name').value.trim(),
    cmd: $('#r_cmd').value.trim(),
    entrypoint: $('#r_entrypoint').value.trim(),
    workdir: $('#r_workdir').value.trim(),
    network: $('#r_network').value.trim(),
    restart: $('#r_restart').value,
    tty: $('#r_tty').checked,
    stdin: $('#r_stdin').checked,
    autoremove: $('#r_autoremove').checked,
    ports: readInputRows($('#r_ports')).filter(Boolean),
    env: readInputRows($('#r_envs')).filter(Boolean),
    volumes: readInputRows($('#r_vols')).filter(Boolean),
  };
  if (!spec.image) { toast('Image is required', 'err'); return; }
  try {
    const r = await api('/api/containers/run', {method:'POST', body: JSON.stringify(spec)});
    toast(`Started ${shortId(r.id)}`, 'ok');
    closeDlg('runDlg');
    setTab('containers');
    refresh();
  } catch (e) { toast(e.message, 'err'); }
});

/* ========== Pull dialog ========== */
let pullSrc = null;
function openPullDialog() {
  $('#p_ref').value = '';
  $('#p_log').textContent = '';
  openDlg('pullDlg');
}
$('#pullGo').addEventListener('click', () => {
  const ref = $('#p_ref').value.trim();
  if (!ref) { toast('Reference required', 'err'); return; }
  if (pullSrc) { try { pullSrc.close(); } catch(_){} pullSrc = null; }
  $('#p_log').textContent = '';
  pullSrc = new EventSource(`/api/images/pull?ref=${encodeURIComponent(ref)}`);
  pullSrc.onmessage = ev => {
    const line = ev.data;
    let txt = line;
    try {
      const o = JSON.parse(line);
      txt = (o.status || '') + (o.id ? ` ${o.id}` : '') + (o.progress ? ` ${o.progress}` : '');
      if (o.error || o.errorDetail) txt = `ERROR: ${o.error || o.errorDetail.message}`;
    } catch (_) {}
    $('#p_log').textContent += txt + '\n';
    $('#p_log').scrollTop = $('#p_log').scrollHeight;
  };
  pullSrc.addEventListener('error', ev => {
    const data = ev.data || '';
    if (data) $('#p_log').textContent += '[error] ' + data + '\n';
  });
  pullSrc.addEventListener('done', () => {
    pullSrc.close(); pullSrc = null;
    toast('Pull complete', 'ok');
    refresh();
  });
});
document.getElementById('pullDlg').addEventListener('close', () => {
  if (pullSrc) { try { pullSrc.close(); } catch(_){} pullSrc = null; }
});

/* ========== Logs dialog ========== */
let logSrc = null;
function openLogs(id, name) {
  $('#logsTitle').textContent = 'Logs · ' + (name || shortId(id));
  $('#logBox').textContent = '';
  if (logSrc) { try { logSrc.close(); } catch(_){} logSrc = null; }
  logSrc = new EventSource(`/api/containers/${id}/logs?tail=500`);
  logSrc.onmessage = ev => {
    $('#logBox').textContent += ev.data + '\n';
    if ($('#logAuto').checked) $('#logBox').scrollTop = $('#logBox').scrollHeight;
  };
  logSrc.addEventListener('done', () => { if (logSrc) { logSrc.close(); logSrc = null; } });
  logSrc.addEventListener('error', () => {});
  openDlg('logsDlg');
}
$('#logClear').addEventListener('click', () => $('#logBox').textContent = '');
document.getElementById('logsDlg').addEventListener('close', () => {
  if (logSrc) { try { logSrc.close(); } catch(_){} logSrc = null; }
});

/* ========== Inspect dialog ========== */
async function openInspect(kind, id, label) {
  $('#inspectTitle').textContent = 'Inspect · ' + (label || id);
  $('#inspectBody').textContent = 'loading...';
  openDlg('inspectDlg');
  const path = kind === 'container' ? `/api/containers/${id}/inspect`
            : kind === 'image' ? `/api/images/${encodeURIComponent(id)}/inspect`
            : kind === 'volume' ? `/api/volumes/${encodeURIComponent(id)}/inspect`
            : `/api/networks/${encodeURIComponent(id)}/inspect`;
  try {
    const data = await api(path);
    $('#inspectBody').textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    $('#inspectBody').textContent = 'Error: ' + e.message;
  }
}

/* ========== Terminal (xterm + WebSocket) ========== */
let term = null, fit = null, ws = null, currentCid = null;
function openTerminal(id, name) {
  currentCid = id;
  $('#termTitle').textContent = 'Terminal · ' + (name || shortId(id));
  $('#termStatus').textContent = 'connecting...';
  if (!term) {
    term = new Terminal({
      fontFamily:'ui-monospace, Menlo, Consolas, monospace',
      fontSize:13, cursorBlink:true, convertEol:false,
      theme:{background:'#000000', foreground:'#cfd2d5'}
    });
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open($('#termhost'));
    term.onData(d => { if (ws && ws.readyState === 1) ws.send(d); });
    term.onResize(({cols, rows}) => {
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({type:'resize', cols, rows}));
    });
  } else {
    term.reset();
  }
  openDlg('termDlg');
  // wait one tick for layout
  setTimeout(() => { try { fit.fit(); } catch(_){} startTermWS(); }, 60);
}
function startTermWS() {
  if (ws) { try { ws.close(); } catch(_){} ws = null; }
  const cmd = $('#termCmd').value || '/bin/sh';
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/exec/${currentCid}?cmd=${encodeURIComponent(cmd)}`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    $('#termStatus').textContent = 'connected';
    if (term && fit) {
      try { fit.fit(); } catch(_){}
      ws.send(JSON.stringify({type:'resize', cols: term.cols, rows: term.rows}));
    }
  };
  ws.onmessage = ev => {
    if (typeof ev.data === 'string') {
      term.write(ev.data);
    } else {
      term.write(new Uint8Array(ev.data));
    }
  };
  ws.onerror = () => { $('#termStatus').textContent = 'error'; };
  ws.onclose = () => {
    $('#termStatus').textContent = 'disconnected';
    term && term.write('\r\n\x1b[33m[connection closed]\x1b[0m\r\n');
  };
}
$('#termRestart').addEventListener('click', () => startTermWS());
document.getElementById('termDlg').addEventListener('close', () => {
  if (ws) { try { ws.close(); } catch(_){} ws = null; }
});
window.addEventListener('resize', () => { if (term && fit && document.getElementById('termDlg').open) { try { fit.fit(); } catch(_){} } });

/* ========== Create dialog (volume/network) ========== */
let createKind = 'volume';
function openCreateDlg(kind) {
  createKind = kind;
  $('#createTitle').textContent = 'Create ' + kind;
  $('#c_name').value = '';
  $('#c_driver').value = kind === 'volume' ? 'local' : 'bridge';
  openDlg('createDlg');
}
$('#createGo').addEventListener('click', async () => {
  const Name = $('#c_name').value.trim();
  const Driver = $('#c_driver').value.trim() || (createKind === 'volume' ? 'local' : 'bridge');
  if (!Name) { toast('Name required', 'err'); return; }
  try {
    if (createKind === 'volume') {
      await api('/api/volumes', {method:'POST', body: JSON.stringify({Name, Driver})});
      toast('volume created', 'ok');
      closeDlg('createDlg');
      refresh();
    } else {
      const r = await api('/api/networks', {method:'POST', body: JSON.stringify({Name, Driver})});
      toast('network created', 'ok');
      closeDlg('createDlg');
      refresh();
      // Auto-open the manage dialog so the user can attach containers in one flow
      if (r && r.Id) openNetworkManage(r.Id, Name);
    }
  } catch (e) { toast(e.message, 'err'); }
});

/* ========== Network manage (attach/detach containers) ========== */
async function openNetworkManage(nid, name) {
  $('#netTitle').textContent = 'Network: ' + name;
  $('#netList').innerHTML = '<tr><td colspan="5" class="empty">Loading...</td></tr>';
  openDlg('netDlg');
  try {
    const [net, containers] = await Promise.all([
      api(`/api/networks/${encodeURIComponent(nid)}/inspect`),
      api('/api/containers'),
    ]);
    const attached = net.Containers || {};
    if (!containers.length) {
      $('#netList').innerHTML = '<tr><td colspan="5" class="empty">No containers exist</td></tr>';
      return;
    }
    const rows = containers.map(c => {
      const cid = c.Id;
      const cname = (c.Names || ['/?'])[0].replace(/^\//, '');
      const ep = attached[cid] || null;
      const isAttached = !!ep;
      const stateName = (c.State || '').toLowerCase();
      const ipv4 = ep ? (ep.IPv4Address || '').split('/')[0] : '';
      return `<tr data-cid="${esc(cid)}">
        <td><input type="checkbox" class="netchk" ${isAttached ? 'checked' : ''}/></td>
        <td>${esc(cname)}</td>
        <td>${esc(c.Image)}</td>
        <td><span class="pill ${stateName}">${esc(stateName)}</span></td>
        <td class="id">${esc(ipv4 || '-')}</td>
      </tr>`;
    }).join('');
    $('#netList').innerHTML = rows;
    $$('#netList .netchk').forEach(chk => {
      chk.addEventListener('change', async () => {
        const tr = chk.closest('tr');
        const cid = tr.dataset.cid;
        const wantAttached = chk.checked;
        const action = wantAttached ? 'connect' : 'disconnect';
        chk.disabled = true;
        try {
          await api(`/api/networks/${encodeURIComponent(nid)}/${action}`, {
            method: 'POST',
            body: JSON.stringify({container: cid, force: true}),
          });
          toast(`Container ${action}ed`, 'ok');
          // Refresh just the IP cell for this row
          try {
            const fresh = await api(`/api/networks/${encodeURIComponent(nid)}/inspect`);
            const ep = (fresh.Containers || {})[cid];
            const ip = ep ? (ep.IPv4Address || '').split('/')[0] : '';
            tr.children[4].textContent = ip || '-';
          } catch (_) {}
        } catch (e) {
          toast(e.message, 'err');
          chk.checked = !wantAttached;
        } finally {
          chk.disabled = false;
        }
      });
    });
  } catch (e) {
    $('#netList').innerHTML = `<tr><td colspan="5" class="empty">${esc(e.message)}</td></tr>`;
  }
}

/* ========== boot ========== */
refreshFooter();
setTab('containers');
</script>
</body>
</html>
"""


# =============================================================================
# Main
# =============================================================================

def _print_startup_banner(host, port):
    print("=" * 60, file=sys.stderr)
    print("  Talentia Docker Viewer  -  http://%s:%d" % (host, port), file=sys.stderr)
    print("  Powered by Guardian Of Galaxy", file=sys.stderr)
    print("=" * 60, file=sys.stderr)


def _is_wsl():
    try:
        with open("/proc/version", "r") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _open_browser(url):
    """Open the URL in the user's default browser.

    On WSL, defer to the Windows host browser (wslview / explorer.exe).
    Disabled when TALDOCKER_NO_BROWSER is truthy.
    """
    if os.environ.get("TALDOCKER_NO_BROWSER", "").strip().lower() in ("1", "true", "yes", "on"):
        return

    if _is_wsl():
        for cmd in (["wslview", url], ["explorer.exe", url], ["cmd.exe", "/c", "start", "", url]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except (FileNotFoundError, OSError):
                continue
        print("[WARN] Could not auto-open browser (no wslview/explorer.exe). "
              "Open the URL above manually.", file=sys.stderr)
        return

    try:
        webbrowser.open(url)
    except Exception:
        pass


PID_FILE = os.environ.get("TALDOCKER_PID_FILE") or "/tmp/taldocker.pid"
LOG_FILE = os.environ.get("TALDOCKER_LOG_FILE") or "/tmp/taldocker.log"


def _read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cleanup_pidfile():
    try:
        if _read_pid() == os.getpid():
            os.unlink(PID_FILE)
    except OSError:
        pass


def _stop_daemon():
    pid = _read_pid()
    if not pid:
        print("[taldocker] not running (no PID file)", file=sys.stderr)
        return 1
    if not _pid_alive(pid):
        print(f"[taldocker] stale PID file ({pid}), removing", file=sys.stderr)
        try:
            os.unlink(PID_FILE)
        except OSError:
            pass
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            os.unlink(PID_FILE)
        except OSError:
            pass
        return 0
    print(f"[taldocker] sent SIGTERM to PID {pid}, waiting...", file=sys.stderr)
    for _ in range(50):
        if not _pid_alive(pid):
            print("[taldocker] stopped", file=sys.stderr)
            return 0
        time.sleep(0.1)
    print("[taldocker] still alive after 5s, sending SIGKILL", file=sys.stderr)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass
    return 0


def _status_daemon():
    pid = _read_pid()
    if pid and _pid_alive(pid):
        print(f"[taldocker] running (PID {pid}, log {LOG_FILE})")
        return 0
    print("[taldocker] not running")
    return 3


def _daemonize():
    """Double-fork to detach from controlling terminal (POSIX only)."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    os.chdir("/")
    os.umask(0o022)

    sys.stdout.flush()
    sys.stderr.flush()

    log_fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(null_fd)
    os.close(log_fd)

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()) + "\n")
    atexit.register(_cleanup_pidfile)


def main():
    parser = argparse.ArgumentParser(
        prog="taldocker",
        description="Talentia Docker Viewer - lightweight Docker UI for WSL2.",
    )
    parser.add_argument(
        "-f", "--foreground",
        action="store_true",
        help="Run in foreground (do not detach). Useful for debugging.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("start", "stop", "status", "restart"),
        default="start",
        help="What to do (default: start).",
    )
    args = parser.parse_args()

    if args.command == "stop":
        sys.exit(_stop_daemon())
    if args.command == "status":
        sys.exit(_status_daemon())
    if args.command == "restart":
        _stop_daemon()

    existing = _read_pid()
    if existing and _pid_alive(existing):
        print(f"[taldocker] already running (PID {existing}). "
              f"Use 'taldocker stop' first.", file=sys.stderr)
        sys.exit(1)

    host = os.environ.get("TALDOCKER_HOST", "127.0.0.1")
    port = int(os.environ.get("TALDOCKER_PORT", "8765"))
    sock = os.environ.get("DOCKER_SOCKET", DockerClient.DEFAULT_SOCK)

    try:
        ver = DOCKER.system_version()
        print(f"[OK] Connected to Docker {ver.get('Version','?')} "
              f"(API {ver.get('ApiVersion','?')}) via {sock}", file=sys.stderr)
    except DockerError as e:
        print(f"[WARN] Cannot reach Docker at {sock}: {e.message}", file=sys.stderr)
        print("       The UI will start anyway. Fix Docker access and refresh.", file=sys.stderr)

    _print_startup_banner(host, port)

    try:
        srv = http.server.ThreadingHTTPServer((host, port), AppHandler)
    except OSError as e:
        print(f"[ERR] Cannot bind {host}:{port}: {e}", file=sys.stderr)
        sys.exit(1)

    detach = not args.foreground
    if detach and os.name != "posix":
        print("[WARN] Detach mode requires POSIX. Falling back to foreground.", file=sys.stderr)
        detach = False

    if detach:
        print(f"[taldocker] detaching; logs in {LOG_FILE}, stop with 'taldocker stop'", file=sys.stderr)
        _daemonize()

    def _on_signal(signum, frame):
        threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    browser_host = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::", "::1") else host
    threading.Timer(0.4, _open_browser, args=(f"http://{browser_host}:{port}",)).start()

    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        print("[taldocker] shutdown complete", file=sys.stderr)


if __name__ == "__main__":
    main()

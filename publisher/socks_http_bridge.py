"""Chromium/Playwright 不支持「带用户名密码的 SOCKS5」代理（底层限制）。

本模块在 127.0.0.1 上启动一个**无认证**的本地 HTTP 代理，将 CONNECT 请求经 PySocks 转发到上游 SOCKS5（含认证）。
浏览器只连接 http://127.0.0.1:<port>，从而绕过该限制。

仅监听回环地址；同一 (socks_host, socks_port, user, pass) 复用同一端口。
"""
from __future__ import annotations

import hashlib
import logging
import select
import socket
import threading
from typing import Dict

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_servers: Dict[str, socket.socket] = {}
_ports: Dict[str, int] = {}


def _fp(host: str, port: int, user: str, pw: str) -> str:
    return hashlib.sha256(f"{host}\0{port}\0{user}\0{pw}".encode()).hexdigest()[:40]


def _parse_connect_target(hostport: str) -> tuple[str, int]:
    hp = hostport.strip()
    if hp.startswith("["):
        rb = hp.index("]")
        host = hp[1:rb].strip()
        rest = hp[rb + 1 :].lstrip()
        if not rest.startswith(":"):
            raise ValueError("bad CONNECT target")
        port = int(rest[1:])
        return host, port
    if ":" not in hp:
        raise ValueError("bad CONNECT target")
    host, ps = hp.rsplit(":", 1)
    return host.strip(), int(ps)


def _relay_bidirectional(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            r, _, err = select.select([a, b], [], [a, b], 300.0)
            if err:
                break
            if not r:
                continue
            for s in r:
                try:
                    data = s.recv(262144)
                except OSError:
                    return
                if not data:
                    return
                other = b if s is a else a
                try:
                    other.sendall(data)
                except OSError:
                    return
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def _handle_client(
    client: socket.socket,
    socks_host: str,
    socks_port: int,
    user: str,
    pw: str,
) -> None:
    try:
        client.settimeout(120)
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = client.recv(65536)
            if not chunk:
                return
            buf += chunk
        if b"\r\n\r\n" not in buf:
            try:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            except OSError:
                pass
            return
        sep = buf.index(b"\r\n\r\n")
        header_blob = buf[:sep]
        extra = buf[sep + 4 :]
        first = header_blob.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = first.split()
        if len(parts) < 2:
            try:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            except OSError:
                pass
            return
        if parts[0].upper() != "CONNECT":
            try:
                client.sendall(
                    b"HTTP/1.1 501 Not Implemented\r\n"
                    b"Content-Type: text/plain\r\n\r\n"
                    b"Only CONNECT (HTTPS) is supported by the SOCKS bridge."
                )
            except OSError:
                pass
            return

        try:
            host, port = _parse_connect_target(parts[1])
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 400 Bad Target\r\n\r\n")
            except OSError:
                pass
            return

        import socks  # PySocks

        upstream = socks.socksocket()
        upstream.set_proxy(socks.SOCKS5, socks_host, socks_port, True, user, pw)
        upstream.settimeout(120)
        try:
            upstream.connect((host, port))
        except Exception as e:
            logger.warning("[socks bridge] upstream connect %s:%s via socks5: %s", host, port, e)
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:
                pass
            return

        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except OSError:
            return
        if extra:
            try:
                upstream.sendall(extra)
            except OSError:
                return
        _relay_bidirectional(client, upstream)
    except Exception as e:
        logger.debug("[socks bridge] handle: %s", e)
        try:
            client.close()
        except OSError:
            pass


def _accept_loop(
    srv: socket.socket,
    socks_host: str,
    socks_port: int,
    user: str,
    pw: str,
) -> None:
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            break
        threading.Thread(
            target=_handle_client,
            args=(c, socks_host, socks_port, user, pw),
            daemon=True,
        ).start()


def ensure_local_http_bridge(socks_host: str, socks_port: int, user: str, pw: str) -> str:
    """返回供 Playwright 使用的本地 HTTP 代理 URL（无认证），例如 http://127.0.0.1:12345。"""
    key = _fp(socks_host, socks_port, user, pw)
    with _lock:
        if key in _ports:
            return f"http://127.0.0.1:{_ports[key]}"

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(256)
    port = srv.getsockname()[1]

    th = threading.Thread(
        target=_accept_loop,
        args=(srv, socks_host, socks_port, user, pw),
        daemon=True,
    )
    th.start()

    with _lock:
        if key in _ports:
            try:
                srv.close()
            except OSError:
                pass
            return f"http://127.0.0.1:{_ports[key]}"
        _servers[key] = srv
        _ports[key] = port

    logger.info(
        "[socks bridge] listening 127.0.0.1:%s -> socks5 %s:%s (auth user=%s)",
        port,
        socks_host,
        socks_port,
        user[:3] + "***" if user else "",
    )
    return f"http://127.0.0.1:{port}"

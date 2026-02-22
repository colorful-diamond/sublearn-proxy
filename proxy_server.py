#!/usr/bin/env python3
"""
Lightweight HTTP/CONNECT proxy for Render deployment.
Supports HTTP CONNECT tunneling for HTTPS proxying.
Basic auth protected.
"""

import asyncio
import base64
import os
import signal
import sys
from urllib.parse import urlparse

# Config
PORT = int(os.environ.get("PORT", 10000))
PROXY_USER = os.environ.get("PROXY_USER", "sublearn")
PROXY_PASS = os.environ.get("PROXY_PASS", "Pr0xy@2026!Render")
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "true").lower() == "true"

def check_auth(headers: dict) -> bool:
    if not AUTH_REQUIRED:
        return True
    auth = headers.get("proxy-authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, passwd = decoded.split(":", 1)
        return user == PROXY_USER and passwd == PROXY_PASS
    except Exception:
        return False

def parse_request(data: bytes):
    lines = data.split(b"\r\n")
    if not lines:
        return None, None, None, {}
    request_line = lines[0].decode("utf-8", errors="replace")
    parts = request_line.split(" ")
    if len(parts) < 3:
        return None, None, None, {}
    method, url, version = parts[0], parts[1], parts[2]
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            key, val = line.decode("utf-8", errors="replace").split(":", 1)
            headers[key.strip().lower()] = val.strip()
    return method, url, version, headers

async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

async def handle_connect(host, port, client_reader, client_writer, version):
    """Handle CONNECT method (HTTPS tunneling)."""
    try:
        remote_reader, remote_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=15
        )
    except Exception as e:
        client_writer.write(f"{version} 502 Bad Gateway\r\n\r\n".encode())
        await client_writer.drain()
        return

    client_writer.write(f"{version} 200 Connection Established\r\n\r\n".encode())
    await client_writer.drain()

    t1 = asyncio.create_task(pipe(client_reader, remote_writer))
    t2 = asyncio.create_task(pipe(remote_reader, client_writer))
    await asyncio.gather(t1, t2, return_exceptions=True)

async def handle_http(method, url, version, headers, body, client_writer):
    """Handle regular HTTP proxy request."""
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    try:
        remote_reader, remote_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=15
        )
    except Exception:
        client_writer.write(f"{version} 502 Bad Gateway\r\n\r\n".encode())
        await client_writer.drain()
        return

    # Remove proxy headers, forward request
    skip_headers = {"proxy-authorization", "proxy-connection"}
    header_lines = f"{method} {path} {version}\r\n"
    header_lines += f"Host: {host}\r\n"
    for k, v in headers.items():
        if k not in skip_headers and k != "host":
            header_lines += f"{k}: {v}\r\n"
    header_lines += "\r\n"

    remote_writer.write(header_lines.encode() + body)
    await remote_writer.drain()

    # Stream response back
    try:
        while True:
            data = await remote_reader.read(65536)
            if not data:
                break
            client_writer.write(data)
            await client_writer.drain()
    except Exception:
        pass
    finally:
        remote_writer.close()

async def handle_client(reader, writer):
    try:
        data = await asyncio.wait_for(reader.read(65536), timeout=30)
        if not data:
            writer.close()
            return

        method, url, version, headers = parse_request(data)
        if not method:
            writer.close()
            return

        # Health check endpoint
        if url in ("/health", "/healthz", "/", "/ping"):
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Connection: close\r\n\r\n"
                '{"status":"ok","service":"sublearn-proxy"}\n'
            )
            writer.write(response.encode())
            await writer.drain()
            writer.close()
            return

        # Check auth
        if not check_auth(headers):
            writer.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Proxy-Authenticate: Basic realm=\"SubLearn Proxy\"\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            return

        if method == "CONNECT":
            # HTTPS tunneling
            host_port = url.split(":")
            host = host_port[0]
            port = int(host_port[1]) if len(host_port) > 1 else 443
            await handle_connect(host, port, reader, writer, version)
        else:
            # HTTP proxy
            body_start = data.find(b"\r\n\r\n")
            body = data[body_start + 4:] if body_start >= 0 else b""
            await handle_http(method, url, version, headers, body, writer)

    except asyncio.TimeoutError:
        pass
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
    finally:
        try:
            writer.close()
        except Exception:
            pass

async def main():
    server = await asyncio.start_server(handle_client, "0.0.0.0", PORT)
    print(f"Proxy running on 0.0.0.0:{PORT}")
    print(f"Auth: {'enabled' if AUTH_REQUIRED else 'disabled'}")

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(server)))

    async with server:
        await server.serve_forever()

async def shutdown(server):
    print("Shutting down...")
    server.close()
    await server.wait_closed()
    asyncio.get_event_loop().stop()

if __name__ == "__main__":
    asyncio.run(main())

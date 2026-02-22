#!/usr/bin/env python3
"""
HTTP Relay Proxy for Render deployment.
Works behind Render's Cloudflare load balancer.
Accepts API requests and fetches URLs using Render's outbound IP.
"""

import asyncio
import base64
import json
import os
import signal
import sys
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", 10000))
PROXY_USER = os.environ.get("PROXY_USER", "sublearn")
PROXY_PASS = os.environ.get("PROXY_PASS", "Pr0xy@2026!Render")

def check_auth(headers: dict) -> bool:
    auth = headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, passwd = decoded.split(":", 1)
        return user == PROXY_USER and passwd == PROXY_PASS
    except Exception:
        return False

def parse_request(data: bytes):
    try:
        lines = data.split(b"\r\n")
        request_line = lines[0].decode("utf-8", errors="replace")
        parts = request_line.split(" ")
        if len(parts) < 3:
            return None, None, None, {}, b""
        method, path, version = parts[0], parts[1], parts[2]
        headers = {}
        for line in lines[1:]:
            if not line:
                break
            if b":" in line:
                key, val = line.decode("utf-8", errors="replace").split(":", 1)
                headers[key.strip().lower()] = val.strip()
        body_start = data.find(b"\r\n\r\n")
        body = data[body_start + 4:] if body_start >= 0 else b""
        return method, path, version, headers, body
    except:
        return None, None, None, {}, b""

async def fetch_url(target_url, method="GET", req_headers=None, req_body=None, timeout=30):
    """Fetch a URL using asyncio and return the response."""
    import ssl
    parsed = urlparse(target_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    use_ssl = parsed.scheme == "https"

    try:
        if use_ssl:
            ssl_ctx = ssl.create_default_context()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_ctx), timeout=timeout
            )
        else:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )

        # Build request
        request = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: Mozilla/5.0\r\nAccept: */*\r\nConnection: close\r\n"
        if req_headers:
            skip = {"host", "connection", "authorization", "content-length"}
            for k, v in req_headers.items():
                if k.lower() not in skip:
                    request += f"{k}: {v}\r\n"
        if req_body:
            request += f"Content-Length: {len(req_body)}\r\n"
        request += "\r\n"

        writer.write(request.encode())
        if req_body:
            writer.write(req_body)
        await writer.drain()

        # Read response
        response_data = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
            if not chunk:
                break
            response_data += chunk
            if len(response_data) > 10 * 1024 * 1024:  # 10MB limit
                break

        writer.close()

        # Parse response
        header_end = response_data.find(b"\r\n\r\n")
        if header_end < 0:
            return 502, {}, response_data

        header_part = response_data[:header_end].decode("utf-8", errors="replace")
        body_part = response_data[header_end + 4:]

        lines = header_part.split("\r\n")
        status_line = lines[0]
        status_code = int(status_line.split(" ")[1]) if len(status_line.split(" ")) > 1 else 502

        resp_headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                resp_headers[k.strip().lower()] = v.strip()

        return status_code, resp_headers, body_part
    except Exception as e:
        return 502, {}, json.dumps({"error": str(e)}).encode()

def json_response(data, status=200):
    body = json.dumps(data).encode()
    return (
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + body

async def handle_client(reader, writer):
    try:
        data = await asyncio.wait_for(reader.read(65536), timeout=30)
        if not data:
            writer.close()
            return

        method, path, version, headers, body = parse_request(data)
        if not method:
            writer.close()
            return

        # Health check
        if path in ("/", "/health", "/healthz", "/ping"):
            writer.write(json_response({"status": "ok", "service": "sublearn-proxy", "version": "1.0"}))
            await writer.drain()
            writer.close()
            return

        # IP check
        if path == "/ip":
            import socket
            writer.write(json_response({"note": "Use /fetch?url=https://httpbin.org/ip to see outbound IP"}))
            await writer.drain()
            writer.close()
            return

        # CORS preflight
        if method == "OPTIONS":
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Access-Control-Allow-Origin: *\r\n"
                b"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                b"Access-Control-Allow-Headers: Authorization, Content-Type\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            return

        # Auth check for all proxy endpoints
        if not check_auth(headers):
            writer.write(json_response({"error": "Unauthorized"}, 401))
            await writer.drain()
            writer.close()
            return

        # Fetch endpoint: GET /fetch?url=<target>
        if path.startswith("/fetch"):
            query = path.split("?", 1)[1] if "?" in path else ""
            params = parse_qs(query)
            target_url = params.get("url", [""])[0]

            if not target_url:
                writer.write(json_response({"error": "Missing ?url= parameter"}, 400))
                await writer.drain()
                writer.close()
                return

            status_code, resp_headers, resp_body = await fetch_url(target_url)

            # Return the response
            content_type = resp_headers.get("content-type", "application/octet-stream")
            response = (
                f"HTTP/1.1 {status_code} Proxied\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(resp_body)}\r\n"
                f"X-Proxy-Status: {status_code}\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"Connection: close\r\n\r\n"
            ).encode() + resp_body

            writer.write(response)
            await writer.drain()
            writer.close()
            return

        # POST /relay — full relay with custom method/headers/body
        if path == "/relay" and method == "POST":
            try:
                req_data = json.loads(body)
            except:
                writer.write(json_response({"error": "Invalid JSON body"}, 400))
                await writer.drain()
                writer.close()
                return

            target_url = req_data.get("url", "")
            target_method = req_data.get("method", "GET")
            target_headers = req_data.get("headers", {})
            target_body = req_data.get("body", "").encode() if req_data.get("body") else None

            if not target_url:
                writer.write(json_response({"error": "Missing url field"}, 400))
                await writer.drain()
                writer.close()
                return

            status_code, resp_headers, resp_body = await fetch_url(
                target_url, target_method, target_headers, target_body
            )

            # Return structured response
            try:
                resp_text = resp_body.decode("utf-8", errors="replace")
            except:
                resp_text = base64.b64encode(resp_body).decode()

            result = {
                "status": status_code,
                "headers": resp_headers,
                "body": resp_text[:100000],  # 100KB limit
                "size": len(resp_body)
            }

            writer.write(json_response(result))
            await writer.drain()
            writer.close()
            return

        # Unknown endpoint
        writer.write(json_response({
            "error": "Unknown endpoint",
            "endpoints": {
                "GET /": "Health check",
                "GET /fetch?url=<url>": "Fetch a URL through proxy (Basic auth required)",
                "POST /relay": "Full relay with custom method/headers/body (Basic auth required)"
            }
        }, 404))
        await writer.drain()
    except Exception as e:
        try:
            writer.write(json_response({"error": str(e)}, 500))
            await writer.drain()
        except:
            pass
    finally:
        try:
            writer.close()
        except:
            pass

async def main():
    server = await asyncio.start_server(handle_client, "0.0.0.0", PORT)
    print(f"Relay proxy running on 0.0.0.0:{PORT}")

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(server)))

    async with server:
        await server.serve_forever()

async def shutdown(server):
    print("Shutting down...")
    server.close()
    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Carry an existing authenticated TCP service over an expiring HTTPS tunnel."""
import argparse
import asyncio
import contextlib
import importlib.metadata
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


def duration(value):
    match = re.fullmatch(r"([1-9][0-9]{0,4})([smh]?)", value)
    if not match:
        raise argparse.ArgumentTypeError("timeout must be a positive duration such as 30m")
    seconds = int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]
    if seconds > 14400:
        raise argparse.ArgumentTypeError("TCP timeout cannot exceed 4 hours")
    return seconds


def target(value):
    # Limit this convenience mode to local services; do not turn it into an
    # unauthenticated proxy into other machines on the network.
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
        raise argparse.ArgumentTypeError("TCP mode requires a local port between 1 and 65535")
    return int(value)


def require_websockets():
    try:
        version = int(importlib.metadata.version("websockets").split(".")[0])
        if 15 <= version < 17:
            return
    except importlib.metadata.PackageNotFoundError:
        pass
    raise ValueError("install websockets>=15,<17 in a virtual environment; see README.md")


def websocket_url(value):
    parsed = urlsplit(value if "://" in value else "https://" + value)
    if (parsed.scheme not in {"https", "wss"} or parsed.username or parsed.password
            or not parsed.hostname or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname)):
        raise ValueError("connect requires an HTTPS hostname, without credentials or a path")
    port = f":{parsed.port}" if parsed.port else ""
    return "wss://" + parsed.hostname + port + "/"


async def relay(websocket, port):
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 5)
    except (OSError, asyncio.TimeoutError):
        await websocket.close(1011, "local service unavailable")
        return
    async def to_tcp():
        async for data in websocket:
            if not isinstance(data, bytes):
                await websocket.close(1003, "binary transport required")
                return
            writer.write(data)
            await writer.drain()
    async def from_tcp():
        while data := await reader.read(65536):
            await websocket.send(data)
    tasks = [asyncio.create_task(to_tcp()), asyncio.create_task(from_tcp())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        await websocket.close()


async def expose(port, name, lifetime):
    from websockets.asyncio.server import serve
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)
    deadline = time.monotonic() + lifetime
    print(f"Forwarding TCP 127.0.0.1:{port}; expires in {lifetime}s. Authentication belongs to the target service.", flush=True)
    # The temporary directory contains frpc's private config. Never serve it.
    with tempfile.TemporaryDirectory(prefix="tunnel-tcp-") as temporary:
        os.chmod(temporary, 0o700)
        async with serve(lambda ws: relay(ws, port), "127.0.0.1", 0,
                         origins=[None], compression=None, max_size=1048576,
                         open_timeout=10, close_timeout=1) as server:
            ws_port = server.sockets[0].getsockname()[1]
            environment = dict(os.environ, TMPDIR=temporary)
            script = str(Path(__file__).resolve().with_name("tunnel"))
            command = ["timeout", "--kill-after=2s", f"{max(0.1, deadline-time.monotonic()):.3f}s",
                       "bash", script, "http", str(ws_port)]
            if name:
                command.append(name)
            # An independent GNU timeout bounds frpc even if this relay is
            # killed abruptly. A separate process group lets us clean up every
            # tunnel child on Ctrl+C, startup failure, or normal expiry.
            process = await asyncio.create_subprocess_exec(*command, env=environment, start_new_session=True)
            exited = asyncio.create_task(process.wait())
            stopped = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait([exited, stopped], timeout=max(0, deadline-time.monotonic()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if exited in done and process.returncode not in (0, 124):
                    raise ValueError(f"tunnel client exited with status {process.returncode}")
                if not done or process.returncode == 124:
                    print("Tunnel expired; closing active connections.", flush=True)
            finally:
                # Close sockets first; the service itself remains untouched.
                server.close()
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.shield(exited), 2)
                except asyncio.TimeoutError:
                    pass
                # The leader may have exited before its children; kill the
                # remaining group too, before deleting the credential file.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)


def connect(value):
    from websockets.sync.client import connect as websocket_connect
    # websockets honors the configured HTTPS proxy. Its default SSL contexts
    # verify both HTTPS proxy certificates and the endpoint; SSL_CERT_FILE and
    # SSL_CERT_DIR support managed environments' CA trust.
    with websocket_connect(websocket_url(value), compression=None, max_size=1048576,
                           ssl=ssl.create_default_context(), open_timeout=20, close_timeout=1) as websocket:
        def send_input():
            try:
                while data := sys.stdin.buffer.read1(65536):
                    websocket.send(data)
            except Exception:
                pass  # The receiving loop reports transport errors to SSH.
            finally:
                websocket.close()
        threading.Thread(target=send_input, daemon=True).start()
        for data in websocket:
            if not isinstance(data, bytes):
                raise ValueError("server sent non-binary TCP transport")
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tcp = commands.add_parser("tcp", help="temporarily expose a local authenticated TCP port")
    tcp.add_argument("port", type=target)
    tcp.add_argument("name", nargs="?")
    tcp.add_argument("--timeout", type=duration, default=1800)
    client = commands.add_parser("connect", help="SSH ProxyCommand transport over HTTPS")
    client.add_argument("url")
    args = parser.parse_args()
    try:
        require_websockets()
        if args.command == "tcp":
            asyncio.run(expose(args.port, args.name, args.timeout))
        else:
            connect(args.url)
    except (ValueError, OSError) as error:
        parser.exit(1, f"tunnel: {error}\n")


if __name__ == "__main__":
    main()

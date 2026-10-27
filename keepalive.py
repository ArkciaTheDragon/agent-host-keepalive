#!/usr/bin/env python3
"""Keep a local VS Code Agent Host connected without depending on a session."""

import argparse
import asyncio
import fcntl
import json
import logging
import math
import os
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

import code_fallback
from host_discovery import AmbiguousHostError, Host, discover_hosts, select_host


ROOT = Path(__file__).resolve().parent
PROTOCOL_VERSIONS = ("1.1.0", "1.0.0", "0.10.0", "0.9.0")
WS_LOGGER = logging.getLogger("agent-host-keepalive.websocket")
WS_LOGGER.addHandler(logging.NullHandler())
WS_LOGGER.propagate = False


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{timestamp()} {message}", flush=True)


def write_state(path: Path, **fields: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        with open(temporary, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
            json.dump({"at": timestamp(), "pid": os.getpid(), **fields}, output)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ProtocolError(Exception):
    pass


class HeartbeatTimeout(TimeoutError):
    pass


async def initialize(websocket: ClientConnection, client_id: str) -> str:
    await websocket.send(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "clientId": client_id,
            "clientInfo": {"name": "agent-host-keepalive", "version": "2.0.0"},
            "protocolVersions": list(PROTOCOL_VERSIONS),
            "initialSubscriptions": [],
        },
    }))
    while True:
        try:
            response = json.loads(await websocket.recv())
        except (ValueError, UnicodeError) as error:
            raise ProtocolError("invalid JSON in AHP initialization response") from error
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
            raise ProtocolError("invalid AHP initialization response")
        if response.get("id") != 1:
            continue
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else None
            message = "incompatible AHP protocol" if code == -32005 else "AHP initialization rejected"
            raise ProtocolError(f"{message} (RPC code {code})")
        result = response.get("result")
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if not isinstance(version, str) or version not in PROTOCOL_VERSIONS:
            raise ProtocolError("server selected an unsupported AHP protocol version")
        return version


async def drain(websocket: ClientConnection) -> None:
    async for _message in websocket:
        pass
    raise ConnectionError("Agent Host closed the connection")


async def maintain_connection(
    websocket: ClientConnection, host: Host, state: Path, protocol: str,
    ping_interval: float, ping_timeout: float, retry_count: int,
) -> None:
    connected_at = timestamp()
    fields = {
        **host.public(), "mode": "native", "protocol_version": protocol, "connected_at": connected_at,
        "retry_count": retry_count, "heartbeat_count": 0, "last_heartbeat_at": None,
    }
    write_state(state, status="connected", **fields)
    log(f"connected to Agent Host pid={host.pid} instance={host.instance_id} protocol={protocol}")
    reader = asyncio.create_task(drain(websocket))
    heartbeats = 0
    try:
        while True:
            done, _ = await asyncio.wait({reader}, timeout=ping_interval)
            if done:
                reader.result()
            pong = await websocket.ping()
            try:
                await asyncio.wait_for(pong, timeout=ping_timeout)
            except asyncio.TimeoutError as error:
                raise HeartbeatTimeout("Agent Host did not answer WebSocket Ping in time") from error
            heartbeats += 1
            fields["heartbeat_count"] = heartbeats
            fields["last_heartbeat_at"] = timestamp()
            write_state(state, status="connected", **fields)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


def retry_delay(delay: float) -> float:
    return min(60.0, random.uniform(delay, delay * 1.2))


async def keepalive(args: argparse.Namespace, state: Path) -> int:
    instance_id = None
    client_id = str(uuid4())
    delay = 2.0
    retries = 0
    cli_mode = False
    cli_commit = None
    polls = 0
    last_success = None
    fallback_reason = None
    target_pid = args.host_pid
    while True:
        try:
            hosts = discover_hosts(args.user_data_dir, log)
            host = select_host(hosts, args.host_pid, instance_id)
        except AmbiguousHostError as error:
            write_state(state, status="fatal", reason="ambiguous-host", candidates=error.candidates)
            log(f"fatal: {error}; candidates={json.dumps(error.candidates)}")
            return 2
        except (OSError, ValueError) as error:
            write_state(state, status="fatal", reason="discovery-failed", detail=str(error))
            log(f"fatal: host discovery failed: {error}")
            return 2

        wait = retry_delay(delay)
        if host is None:
            reason = "host-unavailable" if instance_id or args.host_pid else "no-host"
            write_state(
                state, status="waiting-host", reason=reason, instance_id=instance_id,
                host_pid=target_pid, retry_count=retries, retry_seconds=wait,
            )
            log(f"{reason}; checking registry again in {wait:.1f}s")
        else:
            instance_id = host.instance_id
            target_pid = host.pid
            if cli_mode:
                if cli_commit is None:
                    write_state(
                        state, status="preparing-cli-fallback", mode="code-polling",
                        reason=fallback_reason, **host.public(),
                    )
                    log(f"preparing official CLI fallback at {args.cli}")
                    try:
                        cli_commit = await code_fallback.prepare_cli(args.cli, args.startup_timeout, log)
                    except code_fallback.CLIError as error:
                        write_state(
                            state, status="fatal", mode="code-polling", reason="cli-update-failed",
                            detail=str(error), **host.public(),
                        )
                        log(f"fatal: {error}")
                        return 2
                try:
                    await code_fallback.query_host(args.cli, host, args.startup_timeout)
                except code_fallback.CLIError as error:
                    write_state(
                        state, status="reconnecting", mode="code-polling", reason="cli-query-failed",
                        detail=str(error), last_success_at=last_success, poll_count=polls,
                        retry_count=retries + 1, retry_seconds=wait, **host.public(),
                    )
                    log(f"CLI query failed: {error}; retrying in {wait:.1f}s")
                else:
                    polls += 1
                    last_success = timestamp()
                    write_state(
                        state, status="degraded-code-polling", mode="code-polling",
                        fallback_reason=fallback_reason, cli_commit=cli_commit, poll_count=polls,
                        last_success_at=last_success, poll_interval=args.fallback_interval,
                        retry_count=retries, **host.public(),
                    )
                    log(f"CLI keepalive succeeded for pid={host.pid}; next query in {args.fallback_interval:g}s")
                    delay = 2.0
                    await asyncio.sleep(args.fallback_interval)
                    continue
                await asyncio.sleep(wait)
                delay = min(delay * 2, 60)
                retries += 1
                continue
            write_state(state, status="connecting", mode="native", retry_count=retries, **host.public())
            connected_at = None
            try:
                async with connect(
                    host.authenticated_address, proxy=None, ping_interval=None,
                    open_timeout=args.startup_timeout, close_timeout=5, logger=WS_LOGGER,
                ) as websocket:
                    protocol = await asyncio.wait_for(
                        initialize(websocket, client_id), timeout=args.startup_timeout,
                    )
                    connected_at = time.monotonic()
                    await maintain_connection(
                        websocket, host, state, protocol, args.ping_interval, args.ping_timeout, retries,
                    )
            except ProtocolError as error:
                if args.no_cli_fallback:
                    write_state(state, status="fatal", reason="protocol-error", detail=str(error), **host.public())
                    log(f"fatal: {error}")
                    return 2
                cli_mode = True
                fallback_reason = str(error)
                log(f"{error}; switching to official CLI polling")
                delay = 2.0
                continue
            except (OSError, asyncio.TimeoutError, WebSocketException) as error:
                reason = "connection-failed"
                if isinstance(error, HeartbeatTimeout):
                    reason = "heartbeat-timeout"
                elif isinstance(error, InvalidStatus) and error.response.status_code in (401, 403):
                    reason = "authentication-failed"
                if connected_at is not None and time.monotonic() - connected_at >= 60:
                    delay = 2.0
                wait = retry_delay(delay)
                detail = host.safe_error(error) or type(error).__name__
                write_state(
                    state, status="reconnecting", reason=reason, detail=detail,
                    retry_count=retries + 1, retry_seconds=wait, **host.public(),
                )
                log(f"{reason}: {detail}; reconnecting in {wait:.1f}s")
        await asyncio.sleep(wait)
        delay = min(delay * 2, 60)
        retries += 1


async def run(args: argparse.Namespace, state: Path) -> int:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    worker = asyncio.create_task(keepalive(args, state))
    stop_waiter = asyncio.create_task(stopped.wait())
    try:
        done, _ = await asyncio.wait({worker, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            return worker.result()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        write_state(state, status="stopped")
        log("stopped; Agent Host left running")
        return 0
    finally:
        worker.cancel()
        stop_waiter.cancel()
        await asyncio.gather(worker, stop_waiter, return_exceptions=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return seconds


def positive_pid(value: str) -> int:
    pid = int(value)
    if pid < 1:
        raise argparse.ArgumentTypeError("must be a positive PID")
    return pid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-data-dir", type=Path, default=Path.home() / ".config" / "Code")
    parser.add_argument("--host-pid", type=positive_pid, help="select a host when multiple hosts are registered")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "run")
    parser.add_argument("--startup-timeout", type=positive_seconds, default=45, help="connection and AHP initialization timeout")
    parser.add_argument("--ping-interval", type=positive_seconds, default=20, help="seconds between WebSocket Pings")
    parser.add_argument("--ping-timeout", type=positive_seconds, default=10, help="seconds to wait for a Pong")
    parser.add_argument("--cli", type=Path, default=ROOT / "bin" / "code", help="managed official CLI for protocol fallback")
    parser.add_argument("--fallback-interval", type=positive_seconds, default=20, help="seconds between successful CLI queries")
    parser.add_argument("--no-cli-fallback", action="store_true", help="exit on AHP protocol errors instead of using the CLI")
    args = parser.parse_args()
    args.cli = args.cli.expanduser().absolute()
    try:
        args.state_dir.mkdir(parents=True, exist_ok=True)
        with (args.state_dir / "keepalive.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.error(f"another keepalive process holds {args.state_dir / 'keepalive.lock'}")
            return asyncio.run(run(args, args.state_dir / "state.json"))
    except OSError as error:
        log(f"fatal: local I/O failed: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())

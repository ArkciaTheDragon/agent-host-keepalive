import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

import keepalive
import host_discovery


TOKEN = "test-secret+/token"


def registry_entry(host):
    return {
        "schemaVersion": 2, "type": "standalone", "pid": host.pid,
        "instanceId": host.instance_id, "connectionToken": host.token,
        "protocolVersion": "0.1.0",
        "endpoint": {"type": "tcp", "host": host.host, "port": host.port},
    }


def register(root, host, name="host.json"):
    entries = root / "agent-host" / "local-endpoint" / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    path = entries / name
    path.write_text(json.dumps(registry_entry(host)))
    return path


def arguments(root):
    return argparse.Namespace(
        user_data_dir=root, host_pid=None, startup_timeout=0.2,
        ping_interval=0.02, ping_timeout=0.05, no_cli_fallback=False,
        cli=root / "bin" / "code", fallback_interval=0.02,
    )


async def wait_state(path, predicate, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    latest = {}
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            latest = json.loads(path.read_text())
            if predicate(latest):
                return latest
        await asyncio.sleep(0.01)
    raise AssertionError(f"state condition not reached: {latest}")


async def accept_initialization(websocket, version="1.1.0"):
    request = json.loads(await websocket.recv())
    await websocket.send(json.dumps({
        "jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": version},
    }))
    return request


@asynccontextmanager
async def host_server(handler, **options):
    async with serve(
        handler, "127.0.0.1", 0, ping_interval=None, logger=keepalive.WS_LOGGER, **options,
    ) as server:
        port = server.sockets[0].getsockname()[1]
        yield keepalive.Host(os.getpid(), "instance-one", "127.0.0.1", port, TOKEN)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.host = keepalive.Host(os.getpid(), "one", "127.0.0.1", 12345, TOKEN)

    def test_missing_registry_is_empty(self):
        self.assertEqual(keepalive.discover_hosts(self.root), [])

    def test_discovers_live_host_and_filters_stale_entries(self):
        register(self.root, self.host)
        stale = keepalive.Host(99999999, "stale", "127.0.0.1", 12346, TOKEN)
        register(self.root, stale, "stale.json")
        with patch.object(host_discovery, "process_exists", side_effect=lambda pid: pid == os.getpid()):
            self.assertEqual(keepalive.discover_hosts(self.root, keepalive.log), [self.host])

    def test_invalid_entries_are_reported_and_valid_entries_remain(self):
        path = register(self.root, self.host)
        path.with_name("broken.json").write_text("{")
        with patch.object(keepalive, "log") as log:
            self.assertEqual(keepalive.discover_hosts(self.root, keepalive.log), [self.host])
        self.assertIn("warning", log.call_args.args[0])

    def test_unsupported_schema_and_remote_endpoint_are_rejected(self):
        path = register(self.root, self.host)
        data = registry_entry(self.host)
        for change in (
            {"schemaVersion": 1},
            {"endpoint": {"type": "unix", "path": "/tmp/test"}},
            {"endpoint": {"type": "tcp", "host": "192.0.2.1", "port": 12345}},
            {"pid": True},
            {"endpoint": {"type": "tcp", "host": "127.0.0.1", "port": 0}},
        ):
            with self.subTest(change=change):
                path.write_text(json.dumps({**data, **change}))
                with self.assertRaises(ValueError):
                    host_discovery.load_host(path)

    def test_wildcard_and_ipv6_addresses(self):
        for address, expected in (("0.0.0.0", "ws://127.0.0.1:12345/"), ("::", "ws://[::1]:12345/")):
            with self.subTest(address=address):
                host = keepalive.Host(os.getpid(), "one", address, 12345, TOKEN)
                self.assertEqual(host_discovery.load_host(register(self.root, host)).address, expected)

    def test_multiple_hosts_need_selection_and_pin_instance(self):
        other = keepalive.Host(os.getpid() + 1, "two", "127.0.0.1", 12346, TOKEN)
        with self.assertRaises(keepalive.AmbiguousHostError) as caught:
            keepalive.select_host([self.host, other], None, None)
        self.assertNotIn(TOKEN, repr(caught.exception.candidates))
        self.assertEqual(keepalive.select_host([self.host, other], other.pid, None), other)
        self.assertEqual(keepalive.select_host([self.host, other], None, "one"), self.host)
        self.assertIsNone(keepalive.select_host([other], None, "one"))

    def test_conflicting_instances_are_reported(self):
        register(self.root, self.host)
        replacement = keepalive.Host(self.host.pid, "one", "127.0.0.1", 23456, TOKEN)
        register(self.root, replacement, "duplicate.json")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            keepalive.discover_hosts(self.root)

    def test_token_redaction_and_url_encoding(self):
        self.assertNotIn(TOKEN, repr(self.host))
        self.assertNotIn(TOKEN, json.dumps(self.host.public()))
        encoded = self.host.authenticated_address
        self.assertEqual(parse_qs(urlsplit(encoded).query)["tkn"], [TOKEN])
        detail = self.host.safe_error(OSError(encoded + " " + TOKEN))
        self.assertNotIn(TOKEN, detail)
        self.assertNotIn("test-secret", detail)

    def test_private_atomic_state_and_retry_bounds(self):
        path = self.root / "run" / "state.json"
        keepalive.write_state(path, status="connected", heartbeat_count=2)
        self.assertEqual(json.loads(path.read_text())["heartbeat_count"], 2)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(path.parent.iterdir()), [path])
        for delay in (2, 4, 32, 60):
            for _ in range(10):
                self.assertTrue(delay <= keepalive.retry_delay(delay) <= 60)

    def test_positive_numeric_arguments(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                keepalive.positive_seconds(value)
        self.assertEqual(keepalive.positive_seconds("0.2"), 0.2)
        with self.assertRaises(argparse.ArgumentTypeError):
            keepalive.positive_pid("0")

    def test_managed_cli_path_keeps_symlink_location(self):
        target = self.root / "system-code"
        target.write_text("unchanged")
        managed = self.root / "code"
        managed.symlink_to(target)
        argv = [
            "keepalive.py", "--cli", str(managed), "--state-dir", str(self.root / "run"),
        ]
        with patch.object(sys, "argv", argv), patch.object(keepalive, "run", return_value=0) as run:
            self.assertEqual(keepalive.main(), 0)
        self.assertEqual(run.call_args.args[0].cli, managed)
        self.assertEqual(target.read_text(), "unchanged")


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / "run" / "state.json"
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def test_initialization_and_heartbeats_without_sessions(self):
        requests = []

        async def handler(websocket):
            requests.append(await accept_initialization(websocket))
            await websocket.wait_closed()

        async with host_server(handler) as host:
            register(self.root, host)
            task = self.start(keepalive.keepalive(arguments(self.root), self.state))
            state = await wait_state(self.state, lambda value: value.get("heartbeat_count", 0) >= 2)
            self.assertFalse(task.done())
            self.assertEqual(state["protocol_version"], "1.1.0")
            self.assertEqual(requests[0]["params"]["initialSubscriptions"], [])
            self.assertNotIn("session", json.dumps(requests))
            self.assertNotIn(TOKEN, self.state.read_text())
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_reconnects_with_same_client_identity(self):
        requests = []

        async def handler(websocket):
            requests.append(await accept_initialization(websocket))
            if len(requests) == 1:
                await websocket.close()
            else:
                await websocket.wait_closed()

        async with host_server(handler) as host:
            register(self.root, host)
            with patch.object(keepalive, "retry_delay", return_value=0.01):
                task = self.start(keepalive.keepalive(arguments(self.root), self.state))
                state = await wait_state(
                    self.state, lambda value: value.get("heartbeat_count", 0) >= 1 and value.get("retry_count", 0) >= 1,
                )
                self.assertEqual(state["instance_id"], host.instance_id)
                self.assertEqual(requests[0]["params"]["clientId"], requests[1]["params"]["clientId"])
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_authentication_refreshes_registry_credentials(self):
        async def authenticate(connection, request):
            if parse_qs(urlsplit(request.path).query).get("tkn") != [TOKEN]:
                return connection.respond(401, "denied\n")

        async def handler(websocket):
            await accept_initialization(websocket)
            await websocket.wait_closed()

        async with host_server(handler, process_request=authenticate) as host:
            register(self.root, keepalive.Host(host.pid, host.instance_id, host.host, host.port, "stale-secret"))
            with patch.object(keepalive, "retry_delay", return_value=0.05):
                task = self.start(keepalive.keepalive(arguments(self.root), self.state))
                await wait_state(self.state, lambda value: value.get("reason") == "authentication-failed")
                register(self.root, host)
                state = await wait_state(self.state, lambda value: value.get("heartbeat_count", 0) >= 1)
                self.assertGreaterEqual(state["retry_count"], 1)
                self.assertNotIn("stale-secret", self.state.read_text())
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_protocol_incompatibility_is_fatal(self):
        async def handler(websocket):
            request = json.loads(await websocket.recv())
            await websocket.send(json.dumps({
                "jsonrpc": "2.0", "id": request["id"],
                "error": {"code": -32005, "message": "incompatible " + TOKEN},
            }))
            await websocket.wait_closed()

        async with host_server(handler) as host:
            register(self.root, host)
            args = arguments(self.root)
            args.no_cli_fallback = True
            result = await keepalive.keepalive(args, self.state)
            self.assertEqual(result, 2)
            state = json.loads(self.state.read_text())
            self.assertEqual(state["reason"], "protocol-error")
            self.assertNotIn(TOKEN, self.state.read_text())

    async def test_invalid_initialization_responses(self):
        for response in ("{", "[]", '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"99.0.0"}}'):
            with self.subTest(response=response):
                websocket = AsyncMock()
                websocket.recv.return_value = response
                with self.assertRaises(keepalive.ProtocolError):
                    await keepalive.initialize(websocket, "test-client")

    async def test_initialization_has_a_deadline(self):
        async def handler(websocket):
            await websocket.recv()
            await websocket.wait_closed()

        async with host_server(handler) as host:
            register(self.root, host)
            with patch.object(keepalive, "retry_delay", return_value=0.1):
                task = self.start(keepalive.keepalive(arguments(self.root), self.state))
                await wait_state(self.state, lambda value: value.get("reason") == "connection-failed")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_missing_pong_times_out(self):
        websocket = AsyncMock()
        websocket.ping.return_value = asyncio.get_running_loop().create_future()

        async def blocked_reader(_websocket):
            await asyncio.Event().wait()

        host = keepalive.Host(os.getpid(), "one", "127.0.0.1", 12345, TOKEN)
        with patch.object(keepalive, "drain", side_effect=blocked_reader):
            with self.assertRaises(keepalive.HeartbeatTimeout):
                await keepalive.maintain_connection(websocket, host, self.state, "1.1.0", 0.01, 0.02, 0)

    async def test_no_host_waits_and_multiple_hosts_fail(self):
        with patch.object(keepalive, "retry_delay", return_value=0.01):
            task = self.start(keepalive.keepalive(arguments(self.root), self.state))
            await wait_state(self.state, lambda value: value.get("status") == "waiting-host")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        register(self.root, keepalive.Host(os.getpid(), "one", "127.0.0.1", 12345, TOKEN))
        register(self.root, keepalive.Host(os.getpid(), "two", "127.0.0.1", 12346, TOKEN), "other.json")
        self.assertEqual(await keepalive.keepalive(arguments(self.root), self.state), 2)
        self.assertEqual(json.loads(self.state.read_text())["reason"], "ambiguous-host")

    async def test_signal_shutdown_and_single_instance_lock(self):
        command = [
            sys.executable, str(keepalive.ROOT / "keepalive.py"),
            "--user-data-dir", str(self.root), "--state-dir", str(self.state.parent),
        ]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await wait_state(self.state, lambda value: value.get("status") == "waiting-host")
            duplicate = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(duplicate.communicate(), timeout=5)
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn(b"another keepalive process", stderr)
            process.terminate()
            await asyncio.wait_for(process.communicate(), timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(json.loads(self.state.read_text())["status"], "stopped")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


if __name__ == "__main__":
    unittest.main()

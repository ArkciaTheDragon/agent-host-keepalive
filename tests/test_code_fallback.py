import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

import code_fallback
import host_discovery
import keepalive
from test_keepalive import TOKEN, arguments, host_server, register, wait_state


COMMIT = "a" * 40


def release(archive=b"archive"):
    return {
        "version": COMMIT, "productVersion": "1.143.1",
        "url": "https://example.invalid/code.tar.gz",
        "sha256hash": hashlib.sha256(archive).hexdigest(),
    }


def query_result(host, **fields):
    return json.dumps([{
        "host": {"pid": host.pid, "instanceId": host.instance_id},
        "sessions": [], **fields,
    }]).encode()


class DownloadTests(unittest.TestCase):
    def test_release_validation_and_architecture(self):
        for change in ({"url": "http://example.invalid/code"}, {"sha256hash": "bad"}, {"version": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                code_fallback.validate_release({**release(), **change})
        with (
            patch.object(code_fallback.platform, "machine", return_value="aarch64"),
            patch.object(code_fallback.sys, "platform", "linux"),
            patch.object(code_fallback.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(release()).encode())) as request,
        ):
            self.assertEqual(code_fallback.fetch_release()["version"], COMMIT)
            self.assertIn("cli-linux-arm64", request.call_args.args[0])

    def test_verified_archive_and_checksum_failure(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as bundle:
            member = tarfile.TarInfo("code")
            member.size = 6
            bundle.addfile(member, io.BytesIO(b"binary"))
        archive = output.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "code"
            with patch.object(code_fallback.urllib.request, "urlopen", return_value=io.BytesIO(archive)):
                code_fallback.download_cli(path, release(archive))
            self.assertEqual(path.read_bytes(), b"binary")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            with patch.object(code_fallback.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")):
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    code_fallback.download_cli(path, release(archive))
            self.assertEqual(path.read_bytes(), b"binary")

    def test_archive_symlink_is_rejected(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as bundle:
            member = tarfile.TarInfo("code")
            member.type = tarfile.SYMTYPE
            member.linkname = "/bin/sh"
            bundle.addfile(member)
        archive = output.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(code_fallback.urllib.request, "urlopen", return_value=io.BytesIO(archive)):
                with self.assertRaisesRegex(ValueError, "regular file"):
                    code_fallback.download_cli(Path(directory) / "code", release(archive))


class CLIProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        source = register(self.root, keepalive.Host(os.getpid(), "one", "127.0.0.1", 12345, TOKEN))
        self.host = host_discovery.load_host(source)
        self.cli = self.root / "bin" / "code"

    async def test_query_uses_one_private_registry_and_no_token_argument(self):
        profiles = []

        async def command(argv, timeout, payload=None):
            self.assertEqual(argv[:4], [str(self.cli), "agent", "ps", "--json"])
            self.assertNotIn(TOKEN, " ".join(argv))
            self.assertNotIn(quote(TOKEN, safe=""), " ".join(argv))
            profile = Path(argv[-1])
            profiles.append(profile)
            files = list((profile / "agent-host/local-endpoint/entries").iterdir())
            self.assertEqual([path.name for path in files], [self.host.registry_name])
            self.assertEqual(json.loads(files[0].read_text())["connectionToken"], TOKEN)
            self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            return 0, query_result(self.host), b""

        with patch.object(code_fallback, "run_command", side_effect=command):
            await code_fallback.query_host(self.cli, self.host, 1)
        self.assertFalse(profiles[0].exists())

    async def test_exit_zero_still_requires_successful_target_json(self):
        records = [
            b"not json", b"[]", b"{}", query_result(self.host, error=""),
            query_result(self.host, sessions=None),
            json.dumps([{"host": {"pid": self.host.pid, "instanceId": "other"}, "sessions": []}]).encode(),
            json.dumps([{"host": {"pid": self.host.pid, "instanceId": "one"}, "sessions": []}] * 2).encode(),
        ]
        for stdout in records:
            with self.subTest(stdout=stdout):
                with patch.object(code_fallback, "run_command", return_value=(0, stdout, b"")):
                    with self.assertRaises(code_fallback.CLIError):
                        await code_fallback.query_host(self.cli, self.host, 1)

    async def test_nonzero_exit_redacts_credentials(self):
        stderr = (TOKEN + " " + quote(TOKEN, safe="")).encode()
        with patch.object(code_fallback, "run_command", return_value=(1, b"", stderr)):
            with self.assertRaises(code_fallback.CLIError) as caught:
                await code_fallback.query_host(self.cli, self.host, 1)
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertIn("exit code 1", str(caught.exception))

    async def test_latest_installed_cli_skips_download(self):
        self.cli.parent.mkdir()
        self.cli.touch()
        with patch.object(code_fallback, "run_command", side_effect=[
            (0, json.dumps(release()).encode(), b""), (0, COMMIT.encode(), b""),
        ]) as run:
            self.assertEqual(await code_fallback.prepare_cli(self.cli, 1), COMMIT)
        self.assertEqual(run.call_count, 2)

    async def test_broken_cli_is_replaced_and_staging_is_removed(self):
        self.cli.parent.mkdir()
        self.cli.write_bytes(b"old")

        async def command(argv, timeout, payload=None):
            if "--release" in argv:
                return 0, json.dumps(release()).encode(), b""
            if argv[0] == str(self.cli):
                raise PermissionError("not executable")
            if "--download" in argv:
                Path(argv[-1]).write_bytes(b"new")
                return 0, b"", b""
            return 0, COMMIT.encode(), b""

        report = Mock()
        with patch.object(code_fallback, "run_command", side_effect=command):
            self.assertEqual(await code_fallback.prepare_cli(self.cli, 1, report), COMMIT)
        self.assertEqual(self.cli.read_bytes(), b"new")
        self.assertEqual(list(self.cli.parent.iterdir()), [self.cli])
        report.assert_called_once()

    async def test_bad_download_version_preserves_installed_cli(self):
        self.cli.parent.mkdir()
        self.cli.write_bytes(b"old")

        async def command(argv, timeout, payload=None):
            if "--release" in argv:
                return 0, json.dumps(release()).encode(), b""
            if "--download" in argv:
                Path(argv[-1]).write_bytes(b"new")
                return 0, b"", b""
            return 0, b"wrong-version", b""

        with patch.object(code_fallback, "run_command", side_effect=command):
            with self.assertRaisesRegex(code_fallback.CLIError, "does not match"):
                await code_fallback.prepare_cli(self.cli, 1)
        self.assertEqual(self.cli.read_bytes(), b"old")
        self.assertEqual(list(self.cli.parent.iterdir()), [self.cli])

    async def test_timeout_terminates_owned_process(self):
        pidfile = self.root / "pid"
        script = "import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)"
        with self.assertRaises(asyncio.TimeoutError):
            await code_fallback.run_command([sys.executable, "-c", script, str(pidfile)], 0.3)
        pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_cancellation_terminates_owned_process(self):
        pidfile = self.root / "pid"
        script = "import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)"
        task = asyncio.create_task(code_fallback.run_command([sys.executable, "-c", script, str(pidfile)], 60))
        try:
            async def wait_for_pid():
                while not pidfile.exists():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_pid(), timeout=3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class FallbackControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / "run" / "state.json"

    async def incompatible(self, websocket):
        request = json.loads(await websocket.recv())
        await websocket.send(json.dumps({
            "jsonrpc": "2.0", "id": request["id"],
            "error": {"code": -32005, "message": "unsupported"},
        }))
        await websocket.wait_closed()

    async def test_downgrade_prepares_once_refreshes_and_stays_pinned(self):
        async with host_server(self.incompatible) as host:
            path = register(self.root, host)
            observed = []

            async def query(cli, target, timeout):
                observed.append(target)
                if len(observed) == 1:
                    register(self.root, keepalive.Host(
                        host.pid, host.instance_id, host.host, host.port + 1, "rotated-token",
                    ))
                    register(self.root, keepalive.Host(
                        os.getppid(), "other", host.host, host.port + 2, TOKEN,
                    ), "other.json")
                    raise code_fallback.CLIError("temporary failure")

            with (
                patch.object(code_fallback, "prepare_cli", return_value=COMMIT) as prepare,
                patch.object(code_fallback, "query_host", side_effect=query),
                patch.object(keepalive, "retry_delay", return_value=0.01),
            ):
                task = asyncio.create_task(keepalive.keepalive(arguments(self.root), self.state))
                try:
                    snapshot = await wait_state(self.state, lambda value: value.get("poll_count", 0) >= 2)
                    self.assertEqual(snapshot["status"], "degraded-code-polling")
                    self.assertEqual(snapshot["retry_count"], 1)
                    self.assertEqual(observed[1].token, "rotated-token")
                    self.assertTrue(all(target.instance_id == host.instance_id for target in observed))
                    self.assertNotIn("rotated-token", self.state.read_text())
                    prepare.assert_awaited_once()
                    path.unlink()
                    await wait_state(self.state, lambda value: value.get("status") == "waiting-host")
                    self.assertTrue(all(target.instance_id == host.instance_id for target in observed))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_preparation_failure_is_explicit(self):
        async with host_server(self.incompatible) as host:
            register(self.root, host)
            with patch.object(code_fallback, "prepare_cli", side_effect=code_fallback.CLIError("download unavailable")):
                result = await keepalive.keepalive(arguments(self.root), self.state)
            self.assertEqual(result, 2)
            self.assertEqual(json.loads(self.state.read_text())["reason"], "cli-update-failed")

    async def test_network_timeouts_do_not_trigger_cli_updates(self):
        async def handler(websocket):
            await websocket.recv()
            await websocket.wait_closed()

        async with host_server(handler) as host:
            register(self.root, host)
            with patch.object(code_fallback, "prepare_cli", new_callable=AsyncMock) as prepare:
                task = asyncio.create_task(keepalive.keepalive(arguments(self.root), self.state))
                try:
                    await wait_state(self.state, lambda value: value.get("reason") == "connection-failed")
                    prepare.assert_not_called()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import keepalive


CLI = os.environ.get("KEEPALIVE_REAL_CLI")


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def stop_group_children(group):
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == group:
                os.kill(int(entry.name), signal.SIGTERM)
        except (FileNotFoundError, ProcessLookupError):
            continue


@unittest.skipUnless(CLI, "set KEEPALIVE_REAL_CLI to opt into the 305-second real-host test")
class RealHostTests(unittest.TestCase):
    def test_zero_session_host_survives_idle_timeout(self):
        self.exercise_host(fallback=False)

    def test_cli_fallback_renews_zero_session_host(self):
        self.exercise_host(fallback=True)

    def exercise_host(self, fallback):
        cli = CLI
        assert cli is not None
        with tempfile.TemporaryDirectory(prefix="agent-host-keepalive-test-") as directory:
            root = Path(directory)
            profile, state = root / "profile", root / "run" / "state.json"
            client = None
            with (root / "host.log").open("w") as output:
                supervisor = subprocess.Popen([
                    cli, "agent", "host", "--new-instance", "--foreground",
                    "--user-data-dir", str(profile), "--idle-timeout", "300",
                ], stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    deadline = time.monotonic() + 90
                    hosts = []
                    while time.monotonic() < deadline:
                        self.assertIsNone(supervisor.poll(), "isolated supervisor exited during startup")
                        hosts = keepalive.discover_hosts(profile)
                        if hosts:
                            break
                        time.sleep(0.1)
                    self.assertEqual(len(hosts), 1, "isolated host did not register")
                    self.assertEqual(hosts[0].pid, supervisor.pid)

                    def assert_no_sessions():
                        result = subprocess.run([
                            cli, "agent", "ps", "--json", "--user-data-dir", str(profile),
                        ], capture_output=True, text=True, timeout=90, check=True)
                        entries = json.loads(result.stdout)
                        self.assertEqual(len(entries), 1)
                        self.assertEqual(entries[0]["sessions"], [])
                        self.assertNotIn("error", entries[0])

                    assert_no_sessions()
                    command = [sys.executable, "-B", str(keepalive.ROOT / "keepalive.py")]
                    options = []
                    if fallback:
                        managed = root / "bin" / "code"
                        managed.parent.mkdir()
                        shutil.copy2(cli, managed)
                        command = [
                            sys.executable, "-B", "-c",
                            "import keepalive; keepalive.PROTOCOL_VERSIONS=('0.0.0',); raise SystemExit(keepalive.main())",
                        ]
                        options = ["--cli", str(managed)]
                    client = subprocess.Popen([
                        *command, *options,
                        "--user-data-dir", str(profile), "--state-dir", str(state.parent),
                    ], cwd=keepalive.ROOT, stdout=output, stderr=subprocess.STDOUT)
                    deadline = time.monotonic() + 60
                    snapshot = {}
                    status = "degraded-code-polling" if fallback else "connected"
                    while time.monotonic() < deadline:
                        self.assertIsNone(client.poll(), "keepalive exited before connecting")
                        if state.exists():
                            snapshot = json.loads(state.read_text())
                            if snapshot.get("status") == status:
                                break
                        time.sleep(0.1)
                    self.assertEqual(snapshot.get("status"), status, snapshot)
                    print(f"Isolated zero-session host using {status}; checking beyond its 300-second idle timeout.", flush=True)
                    started = time.monotonic()
                    time.sleep(305)
                    elapsed = time.monotonic() - started
                    self.assertIsNone(client.poll())
                    self.assertIsNone(supervisor.poll())
                    snapshot = json.loads(state.read_text())
                    self.assertEqual(snapshot["status"], status)
                    self.assertEqual(snapshot["retry_count"], 0)
                    counter = "poll_count" if fallback else "heartbeat_count"
                    self.assertGreaterEqual(snapshot[counter], 15)
                    self.assertGreaterEqual(elapsed, 305)
                    assert_no_sessions()
                    client.terminate()
                    self.assertEqual(client.wait(timeout=10), 0)
                    self.assertEqual(json.loads(state.read_text())["status"], "stopped")
                    self.assertIsNone(supervisor.poll(), "stopping keepalive stopped the host")
                    print(
                        f"PASS: zero sessions, idle_timeout=300s, connected={elapsed:.1f}s, "
                        f"{counter}={snapshot[counter]}, retries=0; host survives client shutdown.",
                        flush=True,
                    )
                finally:
                    stop_process(client)
                    stop_process(supervisor)
                    stop_group_children(supervisor.pid)

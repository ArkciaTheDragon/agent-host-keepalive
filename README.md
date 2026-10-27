# Agent Host Keepalive

Keep a local VS Code Agent Host connected through an authenticated AHP WebSocket.
Host discovery is automatic, and keepalive works without creating or subscribing to an agent session.
If native AHP initialization becomes incompatible, the official VS Code CLI provides a polling fallback.

## Requirements

- Linux, Python 3.10 or newer, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- A running VS Code Agent Host registered in the selected user-data directory.
- A schema version 2 registry entry with a local TCP endpoint.

## Install and run

```sh
git clone https://github.com/ArkciaTheDragon/agent-host-keepalive.git
cd agent-host-keepalive
uv sync --locked
uv run --locked keepalive.py
```

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`.
uv manages the project environment in `.venv/`; manual activation is optional.
To start from another working directory:

```sh
uv --directory ~/agent-host-keepalive run --locked keepalive.py
```

When exactly one live host is registered, it is selected automatically.
When no host is available, keepalive waits and checks the registry again.
When multiple hosts are available, it prints their PIDs and instance IDs and exits with code `2`.
Select a specific host with:

```sh
uv run --locked keepalive.py --host-pid 12345
```

For a different VS Code user-data directory:

```sh
uv run --locked keepalive.py --user-data-dir /path/to/Code
```

Press Ctrl+C to close the keepalive connection and exit.
The Agent Host is left running.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--user-data-dir` | `~/.config/Code` | VS Code directory containing `agent-host/local-endpoint/entries/`. |
| `--host-pid` | Automatic | Select a particular registered host by PID. |
| `--state-dir` | `run/` beside the script | State file and process lock directory. |
| `--startup-timeout` | `45` | Timeout in seconds for each connection, initialization, CLI release lookup, version check, or query. |
| `--ping-interval` | `20` | Seconds between WebSocket Pings. |
| `--ping-timeout` | `10` | Seconds to wait for a Pong. |
| `--cli` | `bin/code` beside the script | Managed official CLI executable for protocol fallback. May be replaced during an update. |
| `--fallback-interval` | `20` | Seconds to wait after each successful CLI query. |
| `--no-cli-fallback` | Disabled | Exit on native AHP protocol errors without downloading or running the CLI. |

Default runtime paths are relative to the script, independently of the working directory.
Timeouts and intervals must be finite positive numbers.

## Connection lifecycle

- Read the host address and authentication token from the local registry.
- Complete AHP initialization with an empty subscription list.
- Keep the connection open and send WebSocket Pings to detect broken connections.
- Reconnect after transport, authentication, or heartbeat failures, refreshing the registry credentials and address before each attempt.
- Retry with exponential backoff and jitter from 2 to 60 seconds. A connection lasting at least 60 seconds resets the backoff.

The selected instance ID is pinned for the lifetime of the keepalive process.
If that instance disappears, keepalive waits for it and leaves other hosts untouched.
Restart keepalive to select a replacement host with a new instance ID.
Stale entries for exited processes are ignored; malformed or unsupported entries produce warnings.

The native connection requires no session ID, CLI download, or VS Code update.
The client currently supports AHP versions `1.1.0`, `1.0.0`, `0.10.0`, and `0.9.0`.
Transport and authentication failures stay on the native reconnection path.
Native protocol initialization errors activate the CLI fallback, or exit with code `2` when `--no-cli-fallback` is set.
Ambiguous host selection also exits with code `2`.

A persistent client connection prevents idle retirement that depends on the absence of clients.
Network outages, host crashes, and explicit host shutdowns can still interrupt the connection.
Keepalive does not start or restart an Agent Host.

## Official CLI fallback

On a native protocol error, keepalive checks the latest stable official CLI release once.
A matching managed executable is reused.
Otherwise it downloads the Linux x64 or arm64 CLI, verifies the archive SHA-256 and executable commit, and atomically replaces the managed executable.
The system VS Code installation is left unchanged unless you explicitly select its executable with `--cli`.
Release lookup and download require access to Microsoft's update endpoints.
Archive downloads have a 150-second overall timeout; preparation failure is reported as `cli-update-failed` and exits with code `2`.

Fallback runs `code agent ps --json` periodically against the pinned host.
Each query receives a private, owner-only temporary registry containing just the selected host's original registration.
Credentials are kept out of command-line arguments, lifecycle messages, and state files.
The original registry is left untouched.
A successful query must return exactly the selected PID and instance ID, no host error, and a session list.
An empty session list is valid; exit code `0` alone is insufficient.

Successful queries renew idle timers that restart when a client disconnects.
Set the polling interval and query timeout so that the gap between successful queries stays below the host's idle timeout.
For a 300-second idle timeout, the default 20-second polling interval leaves substantial margin.
This mode provides periodic renewal and reports `degraded-code-polling`; it does not maintain a continuous WebSocket connection.
Failed queries use the same 2-to-60-second retry backoff and reread the registry before retrying.
They do not trigger repeated CLI downloads.
Fallback stays active until keepalive is restarted.

Ctrl+C or SIGTERM closes native connections and terminates any owned CLI query or download worker.
Temporary registries and staged downloads are cleaned up.

## Status

Timestamped lifecycle messages are written to standard output.
The latest status is saved with owner-only permissions in `run/state.json` beside the script, unless `--state-dir` is set:

```sh
cat run/state.json
```

Statuses include `waiting-host`, `connecting`, `connected`, `preparing-cli-fallback`, `degraded-code-polling`, `reconnecting`, `fatal`, and `stopped`.
Connected state includes the host PID, instance ID, negotiated protocol, retry count, heartbeat count, and latest successful heartbeat time.
Fallback state includes its reason, managed CLI commit, successful poll count, and latest successful query time.
Authentication tokens are excluded from state and lifecycle messages.
A lock permits one keepalive process per state directory.

## Upgrading from session-based keepalive

Run `uv sync --locked`, remove `--session` from your launch command, and restart keepalive with `uv run --locked keepalive.py`.
`--cli` now selects the executable used only for protocol fallback.
Previously downloaded `bin/code` executables can be reused when their commit matches the latest stable release.
Generated CLI files, virtual environments, runtime state, and Python caches remain excluded from Git.

## Layout

- [`keepalive.py`](keepalive.py): command-line options, native AHP connection, lifecycle, state, and fallback orchestration.
- [`host_discovery.py`](host_discovery.py): registry parsing, host selection, instance binding, and credential redaction.
- [`code_fallback.py`](code_fallback.py): verified CLI installation, cancellable subprocesses, and target-isolated queries.

## Tests

```sh
uv run --locked python -m unittest discover -s tests -v
```

Optional acceptance tests start isolated real hosts with a 300-second idle timeout.
Each test verifies 305 seconds of session-free native keepalive or forced CLI fallback:

```sh
KEEPALIVE_REAL_CLI=/path/to/code uv run --locked python -m unittest discover -s tests -p test_real_host.py -v
```

The test stops only its own keepalive and isolated host processes.

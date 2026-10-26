# Agent Host Keepalive

Keep an AHP session event subscription connected using the latest stable VS Code CLI.
The script runs `code agent logs` and reconnects when the subscription closes.

## Requirements

- Linux x86-64.
- Python 3.8 or newer; no third-party Python packages are required.
- Internet access to the Microsoft VS Code update and download endpoints.
- An existing AHP session accessible through an authenticated VS Code user-data directory.

## Usage

```sh
git clone https://github.com/ArkciaTheDragon/agent-host-keepalive.git
cd agent-host-keepalive
python3 keepalive.py --session 'copilotcli:/<session-id>'
```

Replace `<session-id>` with your session ID.
The script subscribes to an existing session; it does not create a session or sign in for you.
Press Ctrl+C to stop keepalive and its subscription client.

| Option | Default | Description |
| --- | --- | --- |
| `--session` | Required | AHP session URI. |
| `--user-data-dir` | `~/.config/Code` | VS Code user-data directory. |
| `--cli` | `bin/code` beside the script | CLI executable to manage and run. |
| `--state-dir` | `run/` beside the script | State file and process lock directory. |
| `--startup-timeout` | `45` | Seconds to wait for the subscription to connect. |

For example, select a different VS Code user-data directory:

```sh
python3 keepalive.py \
  --session 'copilotcli:/<session-id>' \
  --user-data-dir /path/to/Code
```

## CLI updates and reconnection

- Before the first connection and every reconnection, query the latest stable Linux x64 CLI release.
- Reuse the local executable if its `--version` output contains the release commit.
- Otherwise, download the release, verify its SHA-256 and executable version, and atomically replace the managed CLI.
- The default CLI lives beside the script, independently of the working directory; the system VS Code installation is left unchanged.
- `--cli` overrides the managed path. The executable at that path may be replaced.
- A connected subscription continues without periodic update checks.
- Ordinary disconnects retry with exponential backoff from 2 to 60 seconds. A connection lasting at least 60 seconds resets the backoff.

An update or launch failure exits with code `2`.
Detected protocol incompatibility, missing authentication, and missing sessions also exit with code `2`.
The update check requires network access even when a local CLI is already installed.

## Status

Timestamped lifecycle messages are written to standard output.
The latest status is saved to `run/state.json` beside the script, unless `--state-dir` is set:

```sh
cat run/state.json
```

A lock permits one keepalive process per state directory.
Downloaded executables, runtime state, and Python caches are excluded from Git.

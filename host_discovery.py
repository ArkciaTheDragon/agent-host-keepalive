"""Discover and select local Agent Hosts without exposing their credentials."""

from collections.abc import Callable
from dataclasses import dataclass, field
import ipaddress
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote, quote_plus, urlencode


@dataclass(frozen=True)
class Host:
    pid: int
    instance_id: str
    host: str
    port: int
    token: str = field(repr=False)
    registry_name: str = field(default="", repr=False, compare=False)
    registration: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    @property
    def address(self) -> str:
        hostname = f"[{self.host}]" if ":" in self.host else self.host
        return f"ws://{hostname}:{self.port}/"

    @property
    def authenticated_address(self) -> str:
        return self.address + ("?" + urlencode({"tkn": self.token}) if self.token else "")

    def public(self) -> dict[str, object]:
        return {"host_pid": self.pid, "instance_id": self.instance_id, "endpoint": self.address}

    def safe_error(self, error: Exception) -> str:
        message = str(error)
        if self.token:
            for value in (quote(self.token, safe=""), quote_plus(self.token), self.token):
                message = message.replace(value, "<redacted>")
        return message


class AmbiguousHostError(Exception):
    def __init__(self, hosts: list[Host]):
        self.candidates = [host.public() for host in hosts]
        super().__init__("multiple Agent Hosts found; select one with --host-pid")


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_host(path: Path) -> Host:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schemaVersion") != 2:
        raise ValueError("unsupported registry schema; expected schemaVersion 2")
    pid, instance_id = data.get("pid"), data.get("instanceId")
    if type(pid) is not int or pid < 1 or not isinstance(instance_id, str) or not instance_id:
        raise ValueError("registry entry needs a positive pid and an instanceId")
    endpoint = data.get("endpoint")
    if not isinstance(endpoint, dict) or endpoint.get("type") != "tcp":
        raise ValueError("unsupported endpoint; expected a local tcp endpoint")
    hostname, port = endpoint.get("host"), endpoint.get("port")
    if not isinstance(hostname, str) or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("registry entry has an invalid host or port")
    if hostname != "localhost":
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError as error:
            raise ValueError("registry endpoint must use a local IP address or localhost") from error
        if address.is_unspecified:
            hostname = "::1" if address.version == 6 else "127.0.0.1"
        elif not address.is_loopback:
            raise ValueError("registry endpoint must be local")
    token = data.get("connectionToken", "")
    if not isinstance(token, str):
        raise ValueError("registry entry has an invalid connectionToken")
    return Host(pid, instance_id, hostname, port, token, path.name, data)


def discover_hosts(
    user_data_dir: Path, report: Callable[[str], None] = logging.warning,
) -> list[Host]:
    registry = user_data_dir / "agent-host" / "local-endpoint" / "entries"
    hosts: dict[str, Host] = {}
    try:
        entries = sorted(registry.iterdir())
    except FileNotFoundError:
        return []
    for path in entries:
        if path.suffix != ".json":
            continue
        try:
            host = load_host(path)
        except FileNotFoundError:
            continue
        except ValueError as error:
            report(f"warning: ignoring registry entry {path.name}: {error}")
            continue
        if process_exists(host.pid):
            if host.instance_id in hosts and hosts[host.instance_id] != host:
                raise ValueError("conflicting registry entries for the same instanceId")
            hosts[host.instance_id] = host
    return list(hosts.values())


def select_host(hosts: list[Host], pid: int | None, instance_id: str | None) -> Host | None:
    matches = [
        host for host in hosts
        if (pid is None or host.pid == pid)
        and (instance_id is None or host.instance_id == instance_id)
    ]
    if len(matches) > 1:
        raise AmbiguousHostError(matches)
    return matches[0] if matches else None

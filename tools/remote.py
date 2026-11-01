"""SSH runner for the three lab boards (password auth) plus a tiny local inventory.

Host aliases and credentials live in `tools/hosts.local.json`, which is git-ignored:
credentials must never reach the submitted repository.

Usage:
    python tools/remote.py <alias> "<remote command>"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import paramiko

HOSTS_FILE = Path(__file__).resolve().parent / "hosts.local.json"


def load_hosts() -> dict[str, dict[str, str]]:
    return json.loads(HOSTS_FILE.read_text(encoding="utf-8"))


def run(alias: str, command: str, timeout: float = 25.0) -> int:
    host = load_hosts()[alias]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host["ip"],
            username=host["user"],
            password=host["password"],
            timeout=8,
            banner_timeout=8,
            auth_timeout=8,
            look_for_keys=False,
            allow_agent=False,
        )
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        sys.stdout.write(out)
        if err.strip():
            sys.stdout.write("--- stderr ---\n" + err)
        sys.stdout.write(f"--- exit={code} ---\n")
        return code
    except Exception as exc:  # noqa: BLE001 - surface any transport failure verbatim
        print(f"SSH ERROR: {type(exc).__name__}: {exc}")
        return -1
    finally:
        client.close()


def pull(alias: str, remote_path: str, local_path: str) -> int:
    host = load_hosts()[alias]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host["ip"], port=host.get("port", 22),
                       username=host["user"], password=host["password"],
                       timeout=15.0, banner_timeout=30.0, auth_timeout=30.0)
        sftp = client.open_sftp()
        sftp.get(remote_path, local_path)
        sftp.close()
        print(f"pulled {alias}:{remote_path} -> {local_path}")
        return 0
    finally:
        client.close()


def push(alias: str, local_path: str, remote_path: str) -> int:
    """Upload one local file to a board (SFTP)."""
    host = load_hosts()[alias]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host["ip"], username=host["user"], password=host["password"],
            timeout=8, banner_timeout=8, auth_timeout=8,
            look_for_keys=False, allow_agent=False,
        )
        with client.open_sftp() as sftp:
            sftp.put(local_path, remote_path)
        print(f"pushed {local_path} -> {alias}:{remote_path}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"SFTP ERROR: {type(exc).__name__}: {exc}")
        return -1
    finally:
        client.close()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--pull":
        if len(sys.argv) != 5:
            print("usage: python tools/remote.py --pull <alias> <remote> <local>")
            raise SystemExit(2)
        raise SystemExit(pull(sys.argv[2], sys.argv[3], sys.argv[4]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--push":
        if len(sys.argv) != 5:
            print("usage: python tools/remote.py --push <alias> <local> <remote>")
            raise SystemExit(2)
        raise SystemExit(0 if push(sys.argv[2], sys.argv[3], sys.argv[4]) == 0 else 1)
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(0 if run(sys.argv[1], sys.argv[2]) == 0 else 1)

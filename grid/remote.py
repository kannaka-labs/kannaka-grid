"""One door for every remote step: ssh/scp in batch mode, dry-run aware, testable."""
from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable


class RemoteError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteResult:
    returncode: int
    stdout: str
    stderr: str


SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new"]

# A command may carry a gateway key (a curl to /key/info authenticates with it); the
# log line is the persistent record, so mask any bearer token or sk- key before logging.
_BEARER = re.compile(r"(Bearer\s+)[^\s'\"]+")
_SKKEY = re.compile(r"sk-[A-Za-z0-9._-]{6,}")


def redact(text: str) -> str:
    text = _BEARER.sub(lambda m: m.group(1) + "***", text)
    return _SKKEY.sub("***", text)


class Remote:
    def __init__(self, host: str, *, runner: Callable = subprocess.run, dry_run: bool = False,
                 log: Callable[[str], None] = lambda s: None):
        self.host, self._run, self.dry_run, self._log = host, runner, dry_run, log
        self.commands: list[str] = []

    def run(self, cmd: str, *, timeout: float = 120) -> RemoteResult:
        self.commands.append(cmd)
        self._log(f"[{self.host}] {redact(cmd)}")
        if self.dry_run:
            return RemoteResult(0, "", "")
        p = self._run(["ssh", *SSH_OPTS, self.host, cmd], capture_output=True, text=True, timeout=timeout)
        return RemoteResult(p.returncode, p.stdout or "", p.stderr or "")

    def read(self, path: str) -> str:
        r = self.run(f"cat {shlex.quote(path)}")
        if r.returncode != 0:
            raise RemoteError(f"{self.host}: cat {path}: {r.stderr.strip()}")
        return r.stdout

    def copy_from(self, remote_path: str, local_path: str) -> None:
        self._scp(f"{self.host}:{remote_path}", local_path)

    def copy_to(self, local_path: str, remote_path: str) -> None:
        self._scp(local_path, f"{self.host}:{remote_path}")

    def _scp(self, src: str, dst: str) -> None:
        self.commands.append(f"scp {src} {dst}")
        self._log(f"[scp] {redact(src)} -> {redact(dst)}")
        if self.dry_run:
            return
        p = self._run(["scp", "-q", *SSH_OPTS, src, dst], capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise RemoteError(f"scp {src} -> {dst}: {(p.stderr or '').strip()}")

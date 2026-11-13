"""Drivers: how the runner reaches the system under test. They execute, they never assert."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..models import InterfaceSpec


class DriverError(RuntimeError):
    """The driver itself is broken (crash, timeout, protocol violation) — distinct from a domain error."""


@dataclass
class CallResult:
    output: Any = None
    error: str | None = None
    error_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def view(self) -> dict[str, Any]:
        return {"error": self.error} if self.error is not None else {"output": self.output}


class Driver(Protocol):
    def call(self, op: str, inp: dict[str, Any]) -> CallResult: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...


def expand_argv(argv: list[str], spec_dir_rel: str) -> list[str]:
    out = [a.replace("{python}", sys.executable).replace("{spec_dir}", spec_dir_rel) for a in argv]
    if out and not os.path.isabs(out[0]):
        resolved = shutil.which(out[0])
        if resolved:
            out[0] = resolved
    return out


class JsonlDriver:
    def __init__(self, argv: list[str], cwd: Path, timeout_s: float) -> None:
        self.argv, self.cwd, self.timeout_s = argv, cwd, timeout_s
        self._next_id = 0
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self._start()

    def _start(self) -> None:
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        try:
            self._proc = subprocess.Popen(
                self.argv,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise DriverError(f"cannot start driver {self.argv}: {exc}") from exc
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._proc, self._lines), daemon=True).start()

    @staticmethod
    def _pump(proc: subprocess.Popen[str], lines: queue.Queue[str | None]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def stderr_tail(self, n: int = 2000) -> str:
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()[-n:]

    def call(self, op: str, inp: dict[str, Any]) -> CallResult:
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        assert self._proc and self._proc.stdin
        self._next_id += 1
        rid = self._next_id
        try:
            self._proc.stdin.write(json.dumps({"id": rid, "op": op, "input": inp}) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise DriverError(f"driver died: {exc}\n{self.stderr_tail()}") from exc
        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise DriverError(f"driver timed out after {self.timeout_s}s on op {op!r}")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise DriverError(f"driver exited during op {op!r}\n{self.stderr_tail()}")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray output; real responses are JSON objects
            if not isinstance(msg, dict) or msg.get("id") != rid:
                continue
            if "error" in msg and msg["error"] is not None:
                if msg.get("error_type") == "UnknownOp":
                    raise DriverError(f"driver does not implement op {op!r}")
                return CallResult(error=str(msg["error"]), error_type=msg.get("error_type"))
            return CallResult(output=msg.get("output"))

    def reset(self) -> None:
        res = self.call("__reset__", {})
        if not res.ok:
            raise DriverError(f"driver reset failed: {res.error}")

    def ping(self) -> bool:
        try:
            return self.call("__ping__", {}).output == "pong"
        except DriverError:
            return False

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        self._proc = None


class HttpDriver:
    def __init__(self, spec: InterfaceSpec, cwd: Path, spec_dir_rel: str) -> None:
        if not spec.base_url:
            raise DriverError("http interface needs base_url")
        self.spec = spec
        self._server: subprocess.Popen[bytes] | None = None
        if spec.command:
            self._server = subprocess.Popen(expand_argv(spec.command, spec_dir_rel), cwd=cwd)
        self.client = httpx.Client(base_url=spec.base_url, timeout=spec.timeout_s)
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.spec.timeout_s
        while time.monotonic() < deadline:
            try:
                self.client.get("/")
                return
            except httpx.TransportError:
                time.sleep(0.2)
        raise DriverError(f"server at {self.spec.base_url} not reachable")

    def call(self, op: str, inp: dict[str, Any]) -> CallResult:
        route = self.spec.routes.get(op)
        if route is None:
            raise DriverError(f"no http route for op {op!r}")
        body = dict(inp)
        path = route.path
        for key in list(body):
            token = "{" + key + "}"
            if token in path:
                path = path.replace(token, str(body.pop(key)))
        if route.method in ("GET", "DELETE"):
            resp = self.client.request(route.method, path, params=body)
        else:
            resp = self.client.request(route.method, path, json=body)
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = resp.text
        if resp.status_code >= 400:
            msg = payload.get("error") or payload.get("detail") if isinstance(payload, dict) else payload
            return CallResult(error=str(msg), error_type=f"HTTP{resp.status_code}")
        return CallResult(output=payload)

    def reset(self) -> None:
        if "__reset__" in self.spec.routes:
            self.call("__reset__", {})

    def close(self) -> None:
        self.client.close()
        if self._server and self._server.poll() is None:
            self._server.terminate()


class CliDriver:
    def __init__(self, spec: InterfaceSpec, cwd: Path, spec_dir_rel: str) -> None:
        self.spec, self.cwd, self.spec_dir_rel = spec, cwd, spec_dir_rel

    def call(self, op: str, inp: dict[str, Any]) -> CallResult:
        template = self.spec.cli.get(op)
        if template is None:
            raise DriverError(f"no cli template for op {op!r}")
        argv = [t.format(**{k: v for k, v in inp.items()}) for t in template]
        try:
            proc = subprocess.run(
                expand_argv(argv, self.spec_dir_rel),
                cwd=self.cwd,
                input=json.dumps(inp),
                capture_output=True,
                text=True,
                timeout=self.spec.timeout_s,
                env={**os.environ, "FACTORY_INPUT": json.dumps(inp)},
            )
        except subprocess.TimeoutExpired as exc:
            raise DriverError(f"cli op {op!r} timed out") from exc
        except KeyError as exc:
            raise DriverError(f"cli template for {op!r} needs input field {exc}") from exc
        if proc.returncode != 0:
            return CallResult(error=(proc.stderr or proc.stdout).strip(), error_type=f"exit{proc.returncode}")
        return CallResult(output={"stdout": proc.stdout.rstrip("\n"), "exit_code": proc.returncode})

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


def open_driver(spec: InterfaceSpec, repo: Path, spec_dir_rel: str) -> Driver:
    if spec.kind == "jsonl":
        if not spec.command:
            raise DriverError("jsonl interface needs a command")
        return JsonlDriver(expand_argv(spec.command, spec_dir_rel), repo, spec.timeout_s)
    if spec.kind == "http":
        return HttpDriver(spec, repo, spec_dir_rel)
    return CliDriver(spec, repo, spec_dir_rel)

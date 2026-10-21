"""MCP-сервери на цьому ПК для агента — наприклад, PhotoCraft, у вікні якого видно, що робить агент.

Робот сам запускає їх (stdio, JSON-RPC по рядках) і пробрасує серверу Kru тим самим з'єднанням, що й чат:
без SSH, відкритих портів і ключів. Налаштування — ~/.config/kru-robot/mcp.json
(Windows: %APPDATA%\\kru-robot\\mcp.json):

    {"exchange_dir": "~/Pictures/kru",
     "mcpServers": {"photocraft": {
         "command": "photocraft-cli",
         "args": ["mcp", "--bridge", "127.0.0.1:7878",
                  "--control-token-file", "~/.config/photocraft/control.token"],
         "confirm": false}}}

exchange_dir — тека обміну файлами з агентом: туди він кладе надіслані тобою фото (pc_put_file) і звідти
забирає результат (pc_get_file). У args працюють ~ і {exchange}. Сервер, що впав чи ще не готовий (PhotoCraft
не відкритий), робот перезапускає щохвилини.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .settings import config_path

PROTOCOL = "2025-06-18"
RETRY_MS = 60_000
INIT_TIMEOUT_MS = 30_000
MAX_FILE = 25 * 1024 * 1024
Reply = Callable[[dict[str, Any] | None, str | None], None]  # (результат, помилка)


def relay_config_path() -> Path:
    return config_path().with_name("mcp.json")


def _debug(msg: str) -> None:
    if os.environ.get("KRU_MCP_DEBUG"):
        print(f"[kru-mcp] {msg}", file=sys.stderr, flush=True)


def _expand(value: str, exchange: Path | None) -> str:
    value = str(value).replace("{exchange}", str(exchange or ""))
    return os.path.expanduser(value) if value.startswith("~") else value


class LocalServer(QObject):
    """Один MCP-сервер на ПК у дочірньому процесі."""

    changed = Signal()  # інструменти з'явились або зникли

    def __init__(self, name: str, spec: dict[str, Any], exchange: Path | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name, self.spec, self.exchange = name, spec, exchange
        self.tools: list[dict[str, Any]] = []
        self.ready = False
        self.last_error = ""
        self._proc: QProcess | None = None
        self._buf = b""
        self._next = 0
        self._pending: dict[int, Reply] = {}
        self._stopped = False
        self._retry = QTimer(self, singleShot=True, interval=RETRY_MS)
        self._retry.timeout.connect(self.start)
        self._init_timer = QTimer(self, singleShot=True, interval=INIT_TIMEOUT_MS)
        self._init_timer.timeout.connect(self._init_timeout)

    @property
    def confirm(self) -> bool:
        return self.spec.get("confirm", True) is not False

    def start(self) -> None:
        if self._stopped or (self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning):
            return
        self.ready, self.tools, self._buf = False, [], b""
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        for k, v in (self.spec.get("env") or {}).items():
            env.insert(str(k), _expand(v, self.exchange))
        proc.setProcessEnvironment(env)
        if self.spec.get("cwd"):
            proc.setWorkingDirectory(_expand(self.spec["cwd"], self.exchange))
        proc.readyReadStandardOutput.connect(self._read)
        proc.readyReadStandardError.connect(self._read_err)
        proc.finished.connect(self._finished)
        proc.errorOccurred.connect(self._error)
        self._proc = proc
        args = [_expand(a, self.exchange) for a in self.spec.get("args") or []]
        _debug(f"{self.name}: запускаю {self.spec['command']} {' '.join(args)}")
        proc.start(_expand(self.spec["command"], self.exchange), args)
        self._init_timer.start()
        self._request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {"roots": {"listChanged": False}},
                                     "clientInfo": {"name": "kru-robot", "version": "1"}}, self._initialized)

    def stop(self) -> None:
        self._stopped = True
        self._retry.stop()
        self._init_timer.stop()
        if self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning:
            self._proc.kill()
            self._proc.waitForFinished(2000)

    # ---------------- JSON-RPC ----------------
    def _write(self, msg: dict[str, Any]) -> None:
        if self._proc is not None:
            self._proc.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))

    def _request(self, method: str, params: dict[str, Any], reply: Reply) -> None:
        self._next += 1
        self._pending[self._next] = reply
        self._write({"jsonrpc": "2.0", "id": self._next, "method": method, "params": params})

    def _read(self) -> None:
        self._buf += bytes(self._proc.readAllStandardOutput().data())
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg:
                if "id" in msg:
                    self._answer(msg)
                continue
            reply = self._pending.pop(msg.get("id"), None)
            if reply is None:
                continue
            if err := msg.get("error"):
                reply(None, str(err.get("message", err)) if isinstance(err, dict) else str(err))
            else:
                reply(msg.get("result") or {}, None)

    def _answer(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "ping":
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        elif method == "roots/list" and self.exchange:
            self._write({"jsonrpc": "2.0", "id": msg["id"],
                         "result": {"roots": [{"uri": self.exchange.as_uri(), "name": "exchange"}]}})
        else:
            self._write({"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32601, "message": f"метод {method} не підтримується"}})

    def _read_err(self) -> None:
        text = bytes(self._proc.readAllStandardError().data()).decode("utf-8", "replace").strip()
        if text:
            self.last_error = text.splitlines()[-1][:300]
            _debug(f"{self.name}: {text}")

    def _initialized(self, result, error) -> None:
        if error:
            self.last_error = error
            self._kill()
            return
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._list_tools(None)

    def _list_tools(self, cursor) -> None:
        def got(result, error) -> None:
            if error:
                self.last_error = error
                self._kill()
                return
            self.tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            if result.get("nextCursor"):
                self._list_tools(result["nextCursor"])
                return
            self._init_timer.stop()
            self.ready = True
            _debug(f"{self.name}: готово, {len(self.tools)} інструментів")
            self.changed.emit()

        self._request("tools/list", {"cursor": cursor} if cursor else {}, got)

    def _init_timeout(self) -> None:
        if not self.ready:
            self.last_error = self.last_error or "не відповів за 30 с"
            self._kill()

    def _kill(self) -> None:
        if self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning:
            self._proc.kill()
        else:
            self._finished()

    def _error(self, err) -> None:
        if err == QProcess.ProcessError.FailedToStart:
            self.last_error = f"не вдалося запустити {self.spec['command']}"
            self._finished()

    def _finished(self, *_args) -> None:
        was_ready = self.ready
        self.ready, self.tools = False, []
        self._init_timer.stop()
        pending, self._pending = self._pending, {}
        for reply in pending.values():
            reply(None, f"{self.name} на ПК завершився: {self.last_error or 'невідома причина'}")
        if was_ready:
            self.changed.emit()
        if not self._stopped:
            _debug(f"{self.name}: недоступний ({self.last_error}), спробую знову за хвилину")
            self._retry.start()

    def call(self, tool: str, args: dict[str, Any], reply: Reply) -> None:
        if not self.ready:
            reply(None, f"{self.name} на ПК зараз недоступний: {self.last_error or 'ще запускається'}")
            return
        self._request("tools/call", {"name": tool, "arguments": args}, reply)


class Relay(QObject):
    """Усі MCP-сервери з mcp.json + тека обміну файлами."""

    changed = Signal()

    def __init__(self, parent: QObject | None = None, path: Path | None = None) -> None:
        super().__init__(parent)
        self.servers: dict[str, LocalServer] = {}
        self.exchange: Path | None = None
        self.error = ""
        path = path or relay_config_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            self.error = f"{path}: {e}"
            return
        if data.get("exchange_dir"):
            self.exchange = Path(os.path.expanduser(str(data["exchange_dir"]))).resolve()
            self.exchange.mkdir(parents=True, exist_ok=True)
        servers = data.get("mcpServers") or data.get("servers") or {}
        for name, spec in servers.items() if isinstance(servers, dict) else ():
            if isinstance(spec, dict) and spec.get("command") and spec.get("enabled", True) is not False:
                srv = LocalServer(str(name), spec, self.exchange, self)
                srv.changed.connect(self.changed)
                self.servers[str(name)] = srv

    @property
    def active(self) -> bool:
        return bool(self.servers or self.exchange)

    def start(self) -> None:
        for srv in self.servers.values():
            srv.start()

    def stop(self) -> None:
        for srv in self.servers.values():
            srv.stop()

    def payload(self) -> dict[str, Any]:
        return {"type": "mcp_tools", "exchange": self.exchange is not None,
                "servers": {n: {"tools": s.tools, "confirm": s.confirm} for n, s in self.servers.items() if s.ready}}

    def handle(self, ev: dict[str, Any], send: Callable[[dict[str, Any]], Any]) -> None:
        """Запит сервера (mcp_call / pc_put / pc_get) → відповідь mcp_result."""
        rid = ev.get("id")

        def reply(result, error) -> None:
            send({"type": "mcp_result", "id": rid, **({"error": error} if error else {"result": result or {}})})

        kind = ev.get("type")
        if kind == "mcp_call":
            srv = self.servers.get(str(ev.get("server")))
            if srv is None:
                reply(None, f"на ПК немає MCP-сервера {ev.get('server')}")
                return
            srv.call(str(ev.get("tool")), ev.get("args") if isinstance(ev.get("args"), dict) else {}, reply)
        elif kind in {"pc_put", "pc_get"}:
            try:
                reply(self._file(kind, ev), None)
            except (OSError, ValueError) as e:
                reply(None, str(e))

    def _file(self, kind: str, ev: dict[str, Any]) -> dict[str, Any]:
        if self.exchange is None:
            raise ValueError("на ПК не налаштовано теку обміну (exchange_dir у mcp.json)")
        name = Path(str(ev.get("name") or "")).name
        if not name or name in {".", ".."}:
            raise ValueError("порожнє ім'я файлу")
        path = self.exchange / name
        if kind == "pc_put":
            data = base64.b64decode(str(ev.get("data", "")), validate=True)
            if len(data) > MAX_FILE:
                raise ValueError("файл завеликий")
            path.write_bytes(data)
            return {"path": str(path), "name": name}
        if not path.is_file():
            have = ", ".join(sorted(p.name for p in self.exchange.iterdir() if p.is_file())[:30]) or "порожньо"
            raise ValueError(f"у теці обміну на ПК немає {name} (є: {have})")
        if path.stat().st_size > MAX_FILE:
            raise ValueError("файл завеликий (понад 25 МБ)")
        return {"data": base64.b64encode(path.read_bytes()).decode("ascii"), "name": name}

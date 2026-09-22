from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Task
from .verify import execute


class VerificationBroker:
    """テスト担当からの一度限りの要求で、固定argvを信頼領域側から実行する。"""

    def __init__(self, task: Task, work: Path, output: Path, timeout: int = 900):
        self.task, self.work, self.output = task, work, output
        self.timeout = timeout
        self.cancel_file = output.with_name("BROKER_STOP")
        self.token = secrets.token_urlsafe(32)
        self.result: dict | None = None
        self._lock = threading.Lock()
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.headers.get("Authorization") != f"Bearer {broker.token}":
                    self.send_error(403)
                    return
                with broker._lock:
                    if broker.result is not None:
                        self.send_error(409)
                        return
                    broker.result = execute(
                        broker.task, broker.work, broker.output, broker.timeout, broker.cancel_file
                    )
                    body = json.dumps(
                        {
                            "success": broker.result["success"],
                            "evidence": str(broker.output),
                            "commands": [
                                {
                                    "argv": item.get("argv"),
                                    "exit_code": item.get("exit_code"),
                                    "timed_out": item.get("timed_out"),
                                    "stopped": item.get("stopped"),
                                    "stderr": str(item.get("stderr", ""))[-2000:],
                                }
                                for item in broker.result.get("commands", [])
                            ],
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/verify"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.cancel_file.touch()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.cancel_file.exists():
            self.cancel_file.unlink()

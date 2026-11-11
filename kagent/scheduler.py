"""Vòng nền: hết hạn phê duyệt, học khi đủ episode hoặc có thất bại, biên dịch sau khi học."""
from __future__ import annotations

import logging
import threading

from . import compiler, db, gateway, learning, results
from .config import settings

log = logging.getLogger("kagent.scheduler")


def tick() -> None:
    with db.session() as conn:
        gateway.expire_approvals(conn)
        results.purge(conn)
        for t in conn.execute("SELECT id FROM tenant").fetchall():
            if learning.should_learn(conn, t["id"]):
                result = learning.run_learning(conn, t["id"])
                log.info("tenant %s học: %s", t["id"], {k: v for k, v in result.items() if k != "details"})
                compiler.compile_tenant(conn, t["id"], "scheduler")


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="kagent-scheduler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(settings.scheduler_interval):
            try:
                tick()
            except Exception:
                log.exception("lỗi vòng nền")

    def stop(self) -> None:
        self._stop.set()

"""Vòng nền: hết hạn phê duyệt, kiểm tra kho tri thức mỗi ngày, học khi đủ episode hoặc có thất bại, biên dịch sau khi học."""
from __future__ import annotations

import logging
import threading

from . import compiler, db, dedup, gateway, incidents, learning, lint, results, sources, topology, undo
from .config import settings

log = logging.getLogger("aiops.scheduler")


def tick() -> None:
    with db.session() as conn:
        gateway.expire_approvals(conn)
        results.purge(conn)
        for t in conn.execute("SELECT id FROM tenant").fetchall():
            if lint.due(conn, t["id"]):
                lint.run(conn, t["id"])
                dedup.start(t["id"])  # luồng riêng: gọi LLM theo từng cụm, không làm trễ vòng nền
            sources.tick(conn, t["id"])
            if learning.should_learn(conn, t["id"]):
                result = learning.run_learning(conn, t["id"])
                log.info("tenant %s học: %s", t["id"], {k: v for k, v in result.items() if k != "details"})
                compiler.compile_tenant(conn, t["id"], "scheduler")


ALERT_TICK = 15  # giây; mỗi tenant tự poll theo poll_seconds của mình


def alert_tick() -> None:
    with db.session() as conn:
        incidents.tick(conn)
        undo.tick(conn)  # cùng nhịp với cảnh báo: kiểm tra sau khi chạy dựa vào cảnh báo mới vừa lấy về
        topology.tick(conn)  # cùng nhịp: sự cố mới mở là lý do quét lại (luồng riêng)


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._alerts: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="aiops-scheduler", daemon=True)
        self._thread.start()
        # Luồng riêng: vòng học có thể chạy lâu (gọi LLM), không được làm trễ việc lấy cảnh báo.
        self._alerts = threading.Thread(target=self._alert_loop, name="aiops-alerts", daemon=True)
        self._alerts.start()

    def _alert_loop(self) -> None:
        while not self._stop.wait(ALERT_TICK):
            try:
                alert_tick()
            except Exception:
                log.exception("lỗi vòng cảnh báo")

    def _loop(self) -> None:
        while not self._stop.wait(settings.scheduler_interval):
            try:
                tick()
            except Exception:
                log.exception("lỗi vòng nền")

    def stop(self) -> None:
        self._stop.set()

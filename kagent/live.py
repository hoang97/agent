"""Lượt đang chạy của từng hội thoại, giữ sự kiện (câu hỏi, chữ đang sinh, lệnh) để trang tải lại xem tiếp được.

Một hội thoại có tối đa một lượt đang chạy. Lượt xong thì gỡ khỏi bảng, nhưng người đang theo dõi vẫn giữ tham chiếu tới
lượt đó nên nhận được nốt các sự kiện cuối và tín hiệu kết thúc.
"""
from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field


class StopFlag(threading.Event):
    """Cờ dừng của một lượt; by là người bấm dừng (ghi vào trace của episode)."""

    def __init__(self) -> None:
        super().__init__()
        self.by: str | None = None

    def stop(self, by: str | None) -> None:
        self.by = self.by or by
        self.set()


# Cờ dừng của lượt đang chạy trong luồng này; luồng chạy lệnh song song nhận qua copy_context.
# Connector đọc để ngắt ngay việc chỉ đọc (vd. giết tiến trình kubectl).
current_stop: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar("current_stop", default=None)


def stopping() -> bool:
    flag = current_stop.get()
    return flag is not None and flag.is_set()


@dataclass
class Run:
    id: str
    conversation_id: str
    task: str
    continuation: bool = False
    owner: str | None = None  # người mở lượt (chat); None với lượt nền như điều tra sự cố
    stop: StopFlag = field(default_factory=StopFlag)
    events: list = field(default_factory=list)
    done: bool = False
    cond: threading.Condition = field(default_factory=threading.Condition)

    def emit(self, event: dict) -> None:
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def follow(self, wait: float = 15.0):
        """Phát lại mọi sự kiện đã có rồi chờ sự kiện mới; None nghĩa là chưa có gì mới trong `wait` giây (để gửi ping)."""
        seen = 0
        while True:
            with self.cond:
                if seen >= len(self.events) and not self.done:
                    self.cond.wait(wait)
                batch, done = self.events[seen:], self.done
                seen += len(batch)
            if not batch and not done:
                yield None
            yield from batch
            if done and seen >= len(self.events):
                return


_lock = threading.Lock()
_runs: dict[str, Run] = {}


def start(conversation_id: str, run_id: str, task: str, continuation: bool = False, owner: str | None = None) -> Run:
    run = Run(run_id, conversation_id, task, continuation, owner)
    with _lock:
        _runs[conversation_id] = run
    return run


def finish(run: Run) -> None:
    with run.cond:
        run.done = True
        run.cond.notify_all()
    with _lock:
        if _runs.get(run.conversation_id) is run:
            del _runs[run.conversation_id]


def current(conversation_id: str) -> Run | None:
    with _lock:
        return _runs.get(conversation_id)

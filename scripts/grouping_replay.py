"""Chạy lại việc gom cảnh báo trên dữ liệu cũ để so cài đặt gom trước khi bật (docs/GROUPING-PLAN.md, bước 0).

Đọc alert_event của một tenant từ bản sao DB (chỉ đọc), phát lại theo đúng thứ tự kagent đã nhận: mỗi mốc first_seen là một vòng
poll, cảnh báo hết theo resolved_at. Mỗi cài đặt chạy trên một DB nháp riêng, bằng đúng code gom của kagent và đồng hồ giả.

Nhóm "đúng" là sự cố của cảnh báo trong bản sao, tức là sau khi người đã tách/gộp tay; sự cố không ai sửa coi là đúng.
Chấm bằng precision/recall theo cặp: hai cảnh báo có cùng sự cố hay không.

LLM mặc định tắt (--llm off): không tốn tiền, chỉ đếm số lần lẽ ra đã hỏi; bỏ qua phần LLM đã gom thật nên recall của
mọi cài đặt đều thấp hơn thực tế như nhau. --llm real gọi LLM đã cấu hình (cần --env-file .env), tốn tiền.

Chạy trên bản sao DB như CLAUDE.md hướng dẫn (backup(), không cp), rồi:
  python scripts/grouping_replay.py /dry/kagent.db --tenant team-platform --env prod \\
      --candidate '{"batch": true, "scoring": true, "topology_minutes": 30}'
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kagent import db, llm  # noqa: E402
from kagent.incidents import grouping, ingest, quality  # noqa: E402
from kagent.incidents.config import check_settings  # noqa: E402


class _Offline(llm.HeuristicProvider):
    """Không gọi LLM; đếm số lần kagent lẽ ra đã hỏi."""

    def __init__(self):
        self.calls = 0

    def correlate(self, alert, incidents_, feedback):
        self.calls += 1
        return {"incident_id": None, "score": 0.0, "reason": "replay không gọi LLM"}


class _Counting:
    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def correlate(self, *args, **kwargs):
        self.calls += 1
        return self.inner.correlate(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _open(path: Path, readonly: bool = False) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro" if readonly else str(path)
    conn = sqlite3.connect(uri, uri=readonly, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _tenant(src, name: str | None) -> sqlite3.Row:
    rows = src.execute("SELECT * FROM tenant WHERE ? IS NULL OR id=? OR name=?", (name, name, name)).fetchall()
    if len(rows) != 1:
        raise SystemExit(f"cần đúng một tenant, tìm thấy {len(rows)}: {[r['name'] for r in rows]}; dùng --tenant")
    return rows[0]


@contextlib.contextmanager
def _clock(start: datetime):
    real, now = db.now, [start]
    db.now = lambda: now[0]
    try:
        yield now
    finally:
        db.now = real


def _run(src, tenant, alerts: list[dict], overrides: dict, llm_mode: str) -> dict:
    """Một lần phát lại với cài đặt đã lưu của tenant, ghi đè phần grouping (và các khóa khác) bằng overrides."""
    saved = json.loads(tenant["alert_settings"] or "{}")
    data = {**saved, **{k: v for k, v in overrides.items() if k != "grouping"},
            "grouping": {**saved.get("grouping", {}), **overrides.get("grouping", {})},
            "investigate": False, "summary": {"enabled": False, "idle_minutes": 5}}
    cfg = check_settings(data)
    tid = tenant["id"]
    with tempfile.TemporaryDirectory() as tmp:
        conn = _open(Path(tmp) / "replay.db")
        db.init_schema(conn)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tenant)")]
        row = {k: tenant[k] for k in tenant.keys() if k in cols}
        conn.execute(f"INSERT INTO tenant({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        conn.execute("UPDATE tenant SET alert_settings=? WHERE id=?", (json.dumps(cfg), tid))
        if src.execute("SELECT 1 FROM sqlite_master WHERE name='topology_edge'").fetchone():
            conn.executemany("INSERT INTO topology_edge(tenant_id,env,src,dst,kind,seen_at) VALUES (?,?,?,?,?,?)",
                             [tuple(r) for r in src.execute("SELECT tenant_id,env,src,dst,kind,seen_at FROM topology_edge WHERE tenant_id=?", (tid,))])
        provider = _Offline() if llm_mode == "off" else _Counting(llm.get_provider())
        old = llm.get_provider()
        llm.set_provider(provider)
        steps: dict[str, dict] = {}
        for a in alerts:
            steps.setdefault(a["first_seen"], {"fire": [], "resolve": []})["fire"].append(a)
            if a["resolved_at"]:
                steps.setdefault(a["resolved_at"], {"fire": [], "resolve": []})["resolve"].append(a)
        try:
            with _clock(datetime.fromisoformat(min(steps))) as now:
                last_stats = None
                for at in sorted(steps):
                    now[0] = datetime.fromisoformat(at)
                    if last_stats is None or now[0] - last_stats >= timedelta(minutes=grouping.STATS_TTL // 60):
                        grouping._stats_cache.clear()  # thống kê lịch sử tính lại theo đồng hồ giả, không theo giờ thật
                        last_stats = now[0]
                    new = []
                    with db.tx(conn):
                        for a in steps[at]["resolve"]:
                            g = conn.execute("SELECT id, incident_id, alertname FROM alert_event WHERE id=? AND status='firing'", (a["id"],)).fetchone()
                            if g:
                                ingest.resolve(conn, tid, g, at)
                        for a in steps[at]["fire"]:
                            new.append(ingest.insert(conn, tid, a["source"], {**a, "labels": json.loads(a["labels"])}, at, cfg, alert_id=a["id"]))
                    grouping.group_new(conn, tid, new, cfg)
                    if cfg["auto_merge"]:
                        grouping.auto_merge(conn, tid, cfg)
                predicted = {r["id"]: r["incident_id"] or f"none:{r['id']}" for r in conn.execute("SELECT id, incident_id FROM alert_event")}
                q = quality.grouping(conn, tid, days=36500)
        finally:
            llm.set_provider(old)
            conn.close()
    truth = {a["id"]: a["incident_id"] or f"none:{a['id']}" for a in alerts}
    return {**quality.pairwise(predicted, truth), "incidents": q["incidents"], "compression": q["compression"],
            "unattached": q["unattached"], "llm_calls": provider.calls, "methods": q["methods"]}


def replay(source: Path, tenant: str | None = None, env: str | None = None, days: int | None = None,
           candidates: dict[str, dict] | None = None, llm_mode: str = "off") -> dict:
    """Trả {tên cài đặt: kết quả}. Luôn có "hiện tại" (cài đặt đã lưu của tenant) để so."""
    src = _open(source, readonly=True)
    try:
        t = _tenant(src, tenant)
        since = db.iso(db.now() - timedelta(days=days)) if days else ""
        alerts = [dict(r) for r in src.execute(
            "SELECT * FROM alert_event WHERE tenant_id=? AND (? IS NULL OR env=?) AND first_seen>=? ORDER BY first_seen, rowid",
            (t["id"], env, env, since)).fetchall()]
        if not alerts:
            raise SystemExit("không có cảnh báo nào để phát lại")
        runs = {"hiện tại": {}, **(candidates or {})}
        return {name: _run(src, t, alerts, overrides, llm_mode) for name, overrides in runs.items()}
    finally:
        src.close()


def _table(results: dict) -> str:
    cols = ["alerts", "true_groups", "predicted_groups", "precision", "recall", "f1", "compression", "unattached", "llm_calls"]
    width = max(len(n) for n in results) + 2
    lines = [" " * width + "".join(f"{c:>17}" for c in cols)]
    for name, r in results.items():
        lines.append(f"{name:<{width}}" + "".join(f"{str(r.get(c)):>17}" for c in cols))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", type=Path, help="đường dẫn bản sao kagent.db")
    ap.add_argument("--tenant", help="tên hoặc id tenant (bỏ trống khi DB chỉ có một tenant)")
    ap.add_argument("--env", help="chỉ phát lại một môi trường")
    ap.add_argument("--days", type=int, help="chỉ lấy cảnh báo N ngày gần nhất")
    ap.add_argument("--candidate", action="append", default=[], metavar="JSON",
                    help='cài đặt grouping cần thử, vd. \'{"batch": true, "scoring": true}\'; lặp lại để thử nhiều bộ')
    ap.add_argument("--llm", choices=("off", "real"), default="off")
    ap.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    args = ap.parse_args()
    candidates = {f"thử {i + 1}": {"grouping": json.loads(c)} for i, c in enumerate(args.candidate)}
    results = replay(args.db, args.tenant, args.env, args.days, candidates, args.llm)
    print(json.dumps(results, ensure_ascii=False, indent=1) if args.json else _table(results))
    for i, c in enumerate(args.candidate):
        print(f"thử {i + 1}: {c}")


if __name__ == "__main__":
    main()

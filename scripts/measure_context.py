"""Đo lượng tri thức gửi kèm mỗi lượt và phần đã gửi ở lượt trước, đọc thẳng DB (chỉ đọc, không gọi LLM).

    python scripts/measure_context.py [đường_dẫn/aiops.db] [--top 5] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict

CONTEXT = re.compile(r"<context note=.*?</context>", re.S)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _context_of(messages: list[dict]) -> str:
    """Khối <context> nằm trong tin nhắn user đầu tiên của lượt (các tin user sau là tool_result hoặc nhắc nhở)."""
    for m in messages:
        if m.get("role") == "user":
            found = CONTEXT.findall(_text(m.get("content")))
            return found[0] if found else ""
    return ""


def measure(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT conversation_id, transcript FROM episode WHERE conversation_id IS NOT NULL ORDER BY conversation_id, started_at, rowid").fetchall()
    by_conv: dict[str, list] = defaultdict(list)
    for cid, transcript in rows:
        by_conv[cid].append(json.loads(transcript) if transcript else None)
    out = []
    for cid, turns in by_conv.items():
        seen: set[str] = set()
        total = repeated = transcript_chars = 0
        per_turn = []
        for tr in turns:
            messages = (tr or {}).get("messages") or []
            transcript_chars += len(json.dumps(messages, ensure_ascii=False))
            ctx = _context_of(messages)
            lines = [line for line in ctx.split("\n") if line.startswith("- ")]
            n = sum(len(line) for line in lines)
            d = sum(len(line) for line in lines if line in seen)
            seen.update(lines)
            total += n
            repeated += d
            per_turn.append({"context_chars": len(ctx), "repeated_pct": round(100 * d / n) if n else 0})
        out.append({"conversation_id": cid, "turns": len(turns), "knowledge_chars": total, "repeated_chars": repeated,
                    "repeated_pct": round(100 * repeated / total) if total else 0, "transcript_chars": transcript_chars, "per_turn": per_turn})
    return sorted(out, key=lambda c: -c["turns"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="data/aiops.db")
    ap.add_argument("--top", type=int, default=5, help="số hội thoại dài nhất in chi tiết từng lượt")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    convs = measure(conn)
    if args.json:
        print(json.dumps(convs, ensure_ascii=False, indent=1))
        return
    multi = [c for c in convs if c["turns"] > 1]
    total = sum(c["knowledge_chars"] for c in multi)
    rep = sum(c["repeated_chars"] for c in multi)
    print(f"hội thoại: {len(convs)} (nhiều lượt: {len(multi)})")
    print(f"tri thức ở hội thoại nhiều lượt: {total} ký tự, đã gửi trước đó: {rep} ({round(100 * rep / total) if total else 0}%)")
    for c in convs[:args.top]:
        print(f"\n{c['conversation_id']}  {c['turns']} lượt  tri thức {c['knowledge_chars']}  lặp {c['repeated_pct']}%  transcript {c['transcript_chars']}")
        for i, t in enumerate(c["per_turn"]):
            print(f"  lượt {i + 1:>2}: context {t['context_chars']:>6} ký tự, lặp {t['repeated_pct']:>3}%")


if __name__ == "__main__":
    main()

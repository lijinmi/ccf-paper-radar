#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
stats.py —— 看一眼库里现在有什么

    ./.venv/Scripts/python.exe scripts/stats.py
    ./.venv/Scripts/python.exe scripts/stats.py --venue neurips --year 2026
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "papers.db"
console = Console()

VENUE_LABEL = {"cvpr": "CVPR", "iccv": "ICCV", "eccv": "ECCV",
               "neurips": "NeurIPS", "iclr": "ICLR", "icml": "ICML"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--venue", help="只看某个会议")
    ap.add_argument("--year", type=int, help="只看某年")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    q = lambda s, *a: conn.execute(s, a).fetchall()  # noqa: E731

    total = q("SELECT COUNT(*) n FROM papers")[0]["n"]
    with_venue = q("SELECT COUNT(*) n FROM papers WHERE venue_key IS NOT NULL")[0]["n"]
    accepted = q("SELECT COUNT(*) n FROM papers WHERE venue_status = 'accepted'")[0]["n"]
    console.print(
        f"共 [bold]{total}[/] 篇 | 识别到会议 [bold]{with_venue}[/] 篇 "
        f"| 其中确认录用 [bold green]{accepted}[/] 篇"
    )

    console.print("\n[bold]按月份[/]")
    t = Table(box=None, padding=(0, 2))
    t.add_column("月份"); t.add_column("篇数", justify="right"); t.add_column("确认录用", justify="right")
    for r in q("SELECT substr(published,1,7) m, COUNT(*) n,"
               " SUM(venue_status='accepted') a FROM papers GROUP BY 1 ORDER BY 1 DESC"):
        t.add_row(r["m"], str(r["n"]), str(r["a"] or 0))
    console.print(t)

    console.print("\n[bold]按会议 × 状态[/]")
    t = Table(box=None, padding=(0, 2))
    t.add_column("会议"); t.add_column("年份", justify="right")
    t.add_column("状态"); t.add_column("篇数", justify="right")
    for r in q("SELECT venue_key, venue_year, venue_status, COUNT(*) n FROM papers"
               " WHERE venue_key IS NOT NULL GROUP BY 1,2,3 ORDER BY n DESC"):
        t.add_row(VENUE_LABEL.get(r["venue_key"], r["venue_key"]),
                  str(r["venue_year"] or "-"), r["venue_status"], str(r["n"]))
    console.print(t)

    if args.venue:
        where = "WHERE venue_key = ? AND venue_status = 'accepted'"
        params: list = [args.venue]
        if args.year:
            where += " AND venue_year = ?"
            params.append(args.year)
        rows = q(f"SELECT arxiv_id, title, published, cv_hint, venue_evidence AS evidence FROM papers {where}"
                 f" ORDER BY published DESC LIMIT ?", *params, args.limit)
        console.print(f"\n[bold]{VENUE_LABEL.get(args.venue, args.venue)} 最新 {len(rows)} 篇[/]")
        for r in rows:
            console.print(f"  [dim]{r['published'][:10]}[/] {r['title'][:88]}")
            console.print(f"            [dim]{r['arxiv_id']} · {r['evidence']}[/]")

    conn.close()


if __name__ == "__main__":
    main()

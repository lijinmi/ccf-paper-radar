#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
collect_cvf.py —— 线 A：CVF Open Access 官方档案抓取（CVPR / ICCV / WACV）

为什么必须单独写一个
--------------------
线 B（arXiv）只能捞到"正在放榜"的会议，会开完之后新挂的论文不再写 comment。
CVPR/ICCV 的全部论文只在 CVF 上，所以这部分必须走官方档案。

CVF 的两个坑（2026-11-11 实测）
------------------------------
1. `?day=0` / `?day=1` 一律返回 **HTTP 500**，分页参数在 2026 已经失效。
   只有 `?day=all` 可用，而它是 **10 MB 的单页**，本地 9 KB/s → 约 20 分钟。
   所以列表页必须"下载一次、缓存、断点续传"，不能每次重抓。
2. 详情页反而很快（~2.3s / 7KB），摘要、作者、页码、bibtex 全在里面。
   所以策略是：列表页拉一次拿全量标题 → 详情页并发补摘要（可断点续传）。

三个阶段（可分别重跑，互不阻塞）
--------------------------------
    list      下载并解析列表页  → 缓存 data/cache/cvf/{CONF}.html + 索引
    abstract  并发抓详情页补摘要 → 写 SQLite（可中断，重跑自动跳过已抓的）
    export    导出 data/papers/{venue}-{year}.jsonl，并按标题指纹关联 arXiv ID

用法
----
    ./.venv/Scripts/python.exe scripts/collect_cvf.py --conf CVPR2026 --phase list
    ./.venv/Scripts/python.exe scripts/collect_cvf.py --conf CVPR2026 --phase abstract --limit 20
    ./.venv/Scripts/python.exe scripts/collect_cvf.py --conf CVPR2026 --phase export
    ./.venv/Scripts/python.exe scripts/collect_cvf.py --conf CVPR2026          # 三阶段全跑
"""

from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from rich.console import Console

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache" / "cvf"
EXPORT_DIR = DATA_DIR / "papers"
DB_PATH = DATA_DIR / "papers.db"
VENUES_YAML = ROOT / "venues.yaml"

BASE = "https://openaccess.thecvf.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# CVF 响应头带 Vary: Accept-Encoding，支持 gzip。列表页裸传 12 MB、压缩后约 2.5 MB，
# 实测吞吐差 10 倍（3 篇/s → 30 篇/s）。requests 默认会带，但显式写出来避免被改坏。
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
TIMEOUT = (30, 180)          # (connect, read)：CVF 首字节经常 20s+
LIST_MIN_BYTES = 1_000_000   # 小于这个数说明没下完
RETRIES = 4

console = Console()
_print_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 会议信息
# ---------------------------------------------------------------------------
CONF_RE = re.compile(r"^([A-Z]+)(\d{4})$")
KEY_MAP = {"CVPR": "cvpr", "ICCV": "iccv", "ECCV": "eccv", "WACV": "wacv"}


def conf_info(conf: str) -> dict:
    """CVPR2026 → {key: cvpr, year: 2026, conf: CVPR2026, meet: 2026-06-03}"""
    m = CONF_RE.match(conf.upper())
    if not m or m.group(1) not in KEY_MAP:
        raise SystemExit(f"无法解析会议代号 {conf!r}，应形如 CVPR2026 / ICCV2025 / WACV2027")
    abbr, year = m.group(1), int(m.group(2))
    meet = None
    if VENUES_YAML.exists():
        data = yaml.safe_load(VENUES_YAML.read_text(encoding="utf-8"))
        for v in data.get("venues", []):
            if v.get("abbr", "").upper() == abbr:
                meet = v.get(f"meet_{year}")
                break
    # meet 形如 "2026-06-03/07 丹佛"，取首日作为 published
    pub = None
    if meet:
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", meet)
        pub = dm.group(1) if dm else None
    return {
        "conf": f"{abbr}{year}",
        "abbr": abbr,
        "key": KEY_MAP[abbr],
        "year": year,
        "meet": meet,
        "published": pub,
        "list_url": f"{BASE}/{abbr}{year}?day=all",
        "page_url": f"{BASE}/{abbr}{year}",
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_get(url: str, *, stream: bool = False, retries: int = RETRIES):
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=stream)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(4 * attempt)
                continue
            last = f"HTTP {r.status_code}"
            if r.status_code == 404:
                break
            time.sleep(2 * attempt)
        except requests.RequestException as e:
            last = type(e).__name__
            time.sleep(4 * attempt)
    with _print_lock:
        console.print(f"  [red]放弃[/] {url[:70]} ({last})")
    return None


# ---------------------------------------------------------------------------
# 阶段 1：列表页
# ---------------------------------------------------------------------------
def download_list(info: dict, force: bool = False) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{info['conf']}.html"

    if path.exists() and not force:
        size = path.stat().st_size
        tail = path.open("rb").read()[-2000:].decode("utf-8", "ignore")
        if size > LIST_MIN_BYTES and "</html>" in tail.lower():
            console.print(f"  缓存命中 {path.name}（{size / 1e6:.1f} MB）")
            return path
        console.print(f"  缓存不完整（{size / 1e6:.1f} MB），续传")

    console.print(f"  下载列表页 {info['list_url']}（约 10 MB，可能要 5~20 分钟）")
    tmp = path.with_suffix(".part")
    resume_at = tmp.stat().st_size if tmp.exists() else 0
    headers = dict(HEADERS)
    if resume_at:
        headers["Range"] = f"bytes={resume_at}-"
        console.print(f"  从 {resume_at / 1e6:.1f} MB 处续传")

    r = requests.get(info["list_url"], headers=headers, timeout=TIMEOUT, stream=True)
    if r.status_code not in (200, 206):
        raise SystemExit(f"列表页拉取失败：HTTP {r.status_code}")

    mode = "ab" if r.status_code == 206 else "wb"
    done, t0, last_report = 0, time.time(), 0
    with tmp.open(mode) as f:
        for chunk in r.iter_content(chunk_size=256 * 1024):
            if not chunk:
                continue
            f.write(chunk)
            done += len(chunk)
            if done - last_report > 2_000_000:
                last_report = done
                speed = done / max(time.time() - t0, 0.1) / 1024
                console.print(f"    {done / 1e6:.1f} MB  {speed:.0f} KB/s")
    r.close()

    tmp.replace(path)
    console.print(f"  完成 {path.name}（{path.stat().st_size / 1e6:.1f} MB）")
    return path


# 列表页结构：<dt class="ptitle"><br><a href="...">Title</a></dt> <dd> 作者表单 </dd>
_DT_SPLIT = re.compile(r'<dt class="ptitle">', re.I)
_HREF_TITLE = re.compile(r'<a href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_AUTHOR_INPUT = re.compile(r'name="query_author" value="([^"]+)"')


def parse_list(html_text: str, info: dict) -> list[dict]:
    blocks = _DT_SPLIT.split(html_text)[1:]
    out: list[dict] = []
    seen: set[str] = set()
    for block in blocks:
        m = _HREF_TITLE.search(block)
        if not m:
            continue
        href, title_html = m.group(1), m.group(2)
        if "paper.html" not in href:
            continue
        title = htmllib.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
        title = re.sub(r"\s+", " ", title)
        url = href if href.startswith("http") else BASE + href

        # 作者在紧随其后的 <dd> 里；截到下一个 <dt 之前即可
        authors = [htmllib.unescape(a).strip() for a in _AUTHOR_INPUT.findall(block)]
        if url in seen:
            continue
        seen.add(url)
        out.append({
            "detail_url": url,
            "title": title,
            "authors": authors,
            "title_fp": title_fingerprint(title),
            "venue_key": info["key"],
            "venue_year": info["year"],
            "conf": info["conf"],
        })
    return out


def title_fingerprint(title: str) -> str:
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", title.lower())
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 阶段 2：详情页
# ---------------------------------------------------------------------------
_RE_ABSTRACT = re.compile(r'<div id="abstract">(.*?)</div>', re.S)
_RE_AUTHORS = re.compile(r'<div id="authors">(.*?)</div>', re.S)
_RE_BIBKEY = re.compile(r"@InProceedings\{([^,]+),")
_RE_PAGES = re.compile(r"pp\.\s*([\d\-\u2013]+)")
_RE_PDF = re.compile(r'href="([^"]+\.pdf)"')


def fetch_detail(url: str) -> dict | None:
    r = http_get(url)
    if r is None:
        return None
    h = r.text

    abstract = ""
    m = _RE_ABSTRACT.search(h)
    if m:
        abstract = htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
        abstract = re.sub(r"\s+", " ", abstract).strip()

    authors: list[str] = []
    pages = None
    m = _RE_AUTHORS.search(h)
    if m:
        raw = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        # 形如 "A, B, C; Proceedings of ... (CVPR), 2026, pp. 28156-28166"
        head, _, tail = raw.partition(";")
        authors = [a.strip() for a in head.split(",") if a.strip() and len(a.strip()) > 1]
        pm = _RE_PAGES.search(tail)
        if pm:
            pages = pm.group(1).replace("\u2013", "-")

    bibkey = None
    m = _RE_BIBKEY.search(h)
    if m:
        bibkey = m.group(1).strip()

    pdf = None
    m = _RE_PDF.search(h)
    if m:
        pdf = m.group(1) if m.group(1).startswith("http") else BASE + m.group(1)

    if not abstract:
        return None   # 视为失败，下次重试
    return {"abstract": abstract, "authors_detail": authors, "pages": pages,
            "bibkey": bibkey, "pdf_url": pdf}


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS cvf_papers (
    detail_url   TEXT PRIMARY KEY,
    conf         TEXT NOT NULL,
    venue_key    TEXT NOT NULL,
    venue_year   INTEGER NOT NULL,
    title        TEXT NOT NULL,
    authors      TEXT,           -- JSON array
    abstract     TEXT,
    pages        TEXT,
    pdf_url      TEXT,
    bibkey       TEXT,
    title_fp     TEXT,
    published    TEXT,           -- 会议首日
    fetched_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_cvf_conf ON cvf_papers(conf);
CREATE INDEX IF NOT EXISTS idx_cvf_fp ON cvf_papers(title_fp);
CREATE INDEX IF NOT EXISTS idx_cvf_abstract ON cvf_papers(conf, abstract);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def save_index(conn: sqlite3.Connection, rows: list[dict], published: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        """INSERT INTO cvf_papers (detail_url, conf, venue_key, venue_year, title, authors,
             title_fp, published, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(detail_url) DO UPDATE SET
             title = excluded.title,
             authors = CASE WHEN cvf_papers.authors IS NULL OR cvf_papers.authors = '[]'
                            THEN excluded.authors ELSE cvf_papers.authors END,
             published = COALESCE(cvf_papers.published, excluded.published)""",
        [(r["detail_url"], r["conf"], r["venue_key"], r["venue_year"], r["title"],
          json.dumps(r["authors"], ensure_ascii=False), r["title_fp"], published, now)
         for r in rows],
    )
    conn.commit()


def pending(conn: sqlite3.Connection, conf: str, limit: int | None) -> list[tuple[str, str]]:
    sql = ("SELECT detail_url, title FROM cvf_papers "
           "WHERE conf = ? AND (abstract IS NULL OR abstract = '') ORDER BY rowid")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (conf,)).fetchall()


def save_detail(conn: sqlite3.Connection, url: str, d: dict) -> None:
    conn.execute(
        """UPDATE cvf_papers SET abstract = ?, authors = COALESCE(NULLIF(?, '[]'), authors),
             pages = ?, pdf_url = ?, bibkey = ?, fetched_at = ? WHERE detail_url = ?""",
        (d["abstract"], json.dumps(d["authors_detail"], ensure_ascii=False) if d["authors_detail"] else "[]",
         d["pages"], d["pdf_url"], d["bibkey"],
         datetime.now(timezone.utc).isoformat(timespec="seconds"), url),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 阶段 3：导出
# ---------------------------------------------------------------------------
def load_arxiv_index() -> dict[str, str]:
    """data/papers/YYYY-MM.jsonl（arXiv 分片）→ {title_fp: arxiv_id}"""
    idx: dict[str, str] = {}
    if not EXPORT_DIR.exists():
        return idx
    for f in EXPORT_DIR.glob("*.jsonl"):
        if re.match(r"^\d{4}-\d{2}$", f.stem):        # 只吃 arXiv 的按月分片
            for line in f.open(encoding="utf-8"):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("title_fp") and rec.get("arxiv_id"):
                    idx[rec["title_fp"]] = rec["arxiv_id"]
    return idx


def export(conn: sqlite3.Connection, info: dict, attach_arxiv: bool = True) -> Path:
    rows = conn.execute(
        """SELECT detail_url, conf, venue_key, venue_year, title, authors, abstract, pages,
                  pdf_url, bibkey, title_fp, published
           FROM cvf_papers WHERE conf = ? ORDER BY title""", (info["conf"],)).fetchall()
    arxiv_idx = load_arxiv_index() if attach_arxiv else {}

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = EXPORT_DIR / f"{info['key']}-{info['year']}.jsonl"
    n_linked = 0
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            (url, conf, vkey, vyear, title, authors, abstract, pages,
             pdf, bibkey, fp, published) = r
            arxiv_id = arxiv_idx.get(fp)
            if arxiv_id:
                n_linked += 1
            rec = {
                "source": "cvf",
                "detail_url": url,
                "conf": conf,
                "venue_key": vkey,
                "venue_year": vyear,
                "title": title,
                "title_fp": fp,
                "authors": json.loads(authors) if authors else [],
                "abstract": abstract,
                "pages": pages,
                "pdf_url": pdf,
                "bibkey": bibkey,
                "arxiv_id": arxiv_id,
                "published": published,
                "venue_status": "accepted",
                "confidence": "high",
                "evidence": f"{conf} Open Access",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    console.print(f"  导出 {out.relative_to(ROOT)}  {len(rows)} 篇  "
                  f"{out.stat().st_size / 1024:.0f} KB  （关联到 arXiv：{n_linked} 篇）")
    return out


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="CVF Open Access 抓取（线 A）")
    ap.add_argument("--conf", default="CVPR2026", help="CVPR2026 / ICCV2025 / WACV2027")
    ap.add_argument("--phase", default="all", choices=["all", "list", "abstract", "export"])
    ap.add_argument("--workers", type=int, default=6, help="详情页并发数（别超过 8，CVF 会限速）")
    ap.add_argument("--limit", type=int, default=None, help="本次最多抓多少篇摘要")
    ap.add_argument("--force-download", action="store_true", help="忽略缓存重下列表页")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    info = conf_info(args.conf)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    console.rule(f"[bold]{info['conf']}  ·  线 A")

    conn = sqlite3.connect(args.db, check_same_thread=False)
    # 多线程写同一个 SQLite 连接必须串行化；WAL 让读写不互斥
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)

    # --- 阶段 1 ---
    if args.phase in ("all", "list"):
        path = download_list(info, force=args.force_download)
        html_text = path.read_text(encoding="utf-8", errors="ignore")
        rows = parse_list(html_text, info)
        if not rows:
            console.print("[red]列表页解析出 0 篇，页面结构可能变了[/]")
            return 1
        save_index(conn, rows, info["published"])
        total = conn.execute("SELECT COUNT(*) FROM cvf_papers WHERE conf = ?",
                             (info["conf"],)).fetchone()[0]
        console.print(f"  解析 {len(rows)} 篇（库内累计 {total} 篇）")

    # --- 阶段 2 ---
    if args.phase in ("all", "abstract"):
        todo = pending(conn, info["conf"], args.limit)
        if not todo:
            console.print("  摘要已抓全，跳过")
        else:
            console.print(f"  待抓摘要 {len(todo)} 篇，并发 {args.workers}")
            t0 = time.time()
            done = ok = 0
            lock = threading.Lock()
            db_lock = threading.Lock()

            def work(item):
                url, title = item
                d = fetch_detail(url)
                if d:
                    with db_lock:          # SQLite 写必须串行
                        save_detail(conn, url, d)
                return bool(d)

            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(work, it): it for it in todo}
                for fut in as_completed(futs):
                    success = fut.result()
                    with lock:
                        done += 1
                        ok += 1 if success else 0
                        if done % 20 == 0 or done == len(todo):
                            el = time.time() - t0
                            rate = done / el
                            eta = (len(todo) - done) / rate if rate else 0
                            console.print(
                                f"    {done}/{len(todo)}  成功 {ok}  "
                                f"{rate:.1f} 篇/s  剩余 ~{eta / 60:.0f} 分钟"
                            )
            console.print(f"  本批完成：成功 {ok} / {done}，耗时 {(time.time() - t0) / 60:.1f} 分钟")

    # --- 阶段 3 ---
    if args.phase in ("all", "export"):
        export(conn, info)

    # 汇总
    row = conn.execute(
        """SELECT COUNT(*) n, SUM(abstract IS NOT NULL AND abstract != '') a
           FROM cvf_papers WHERE conf = ?""", (info["conf"],)).fetchone()
    if row and row[0]:
        console.print(f"\n[bold]{info['conf']}[/] 共 {row[0]} 篇，摘要完整 {row[1]} 篇，"
                      f"缺 {row[0] - (row[1] or 0)} 篇")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

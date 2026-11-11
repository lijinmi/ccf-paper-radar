#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
collect_arxiv.py —— 线 B：arXiv 每日增量采集

设计要点
--------
1. 只要"新"，不要"全"：按 submittedDate 倒序翻页，越过时间窗就停。
2. 顺手识别会议归属：arXiv 的 `arxiv:comment` / `journal_ref` 里，作者常自己写
   "Accepted to CVPR 2026"。正则匹配即可，不必等官方名单（约 70~85% 命中）。
3. 只增不改，但允许"升级"：同一篇论文后来补了录用信息，要能更新 venue 字段。
4. 增量靠 SQLite 去重，原始快照另存 JSONL，方便回溯与重放。

用法
----
    ./.venv/Scripts/python.exe scripts/collect_arxiv.py                # 默认 cs.CV 近 3 天
    ./.venv/Scripts/python.exe scripts/collect_arxiv.py --days 7
    ./.venv/Scripts/python.exe scripts/collect_arxiv.py --preset wide  # 含 cs.LG/cs.AI/cs.MM
    ./.venv/Scripts/python.exe scripts/collect_arxiv.py --only-accepted  # 只看已中稿的
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "papers.db"

API = "https://export.arxiv.org/api/query"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
}

# arXiv 官方要求：请求间隔 >= 3 秒，并发 1
REQUEST_INTERVAL = 3.0
PAGE_SIZE = 100
TIMEOUT = 60
UA = "CCFPaperRadar/0.1 (personal research digest; contact: local)"

console = Console()

# ---------------------------------------------------------------------------
# 会议识别
# ---------------------------------------------------------------------------
# 会议别名 → venues.yaml 里的 key
VENUE_ALIASES: list[tuple[str, list[str]]] = [
    ("cvpr", ["CVPR", "Conference on Computer Vision and Pattern Recognition"]),
    ("iccv", ["ICCV", "International Conference on Computer Vision"]),
    ("eccv", ["ECCV", "European Conference on Computer Vision"]),
    ("neurips", ["NeurIPS", "NIPS", "Neural Information Processing Systems"]),
    ("iclr", ["ICLR", "International Conference on Learning Representations"]),
    ("icml", ["ICML", "International Conference on Machine Learning"]),
]

# 别名 → key 的快速查找表
_ALIAS_INDEX: dict[str, str] = {}
for _key, _aliases in VENUE_ALIASES:
    for _a in _aliases:
        _ALIAS_INDEX[_a.lower()] = _key

# 任一会议名的出现（用于快速预筛，避免对每篇都跑全套正则）
# 注意：结尾用 (?![A-Za-z]) 而不是 \b —— "ICLR2027" 里 R 与 2 之间没有 \b
_ANY_VENUE = re.compile(
    r"\b(CVPR|ICCV|ECCV|NeurIPS|NIPS|ICLR|ICML)(?![A-Za-z])"
    r"|Conference on Computer Vision and Pattern Recognition"
    r"|International Conference on Computer Vision\b"
    r"|European Conference on Computer Vision"
    r"|Neural Information Processing Systems"
    r"|International Conference on Learning Representations"
    r"|International Conference on Machine Learning",
    re.IGNORECASE,
)

# 会议名 + 年份的各种写法：CVPR 2026 / CVPR'26 / CVPR2026 / 2026 CVPR
_YEAR = r"(20\d{2}|'\d{2})"
# (正则, 会议在前还是年份在前)
_VENUE_YEAR_PATTERNS = [
    (re.compile(r"\b(CVPR|ICCV|ECCV|NeurIPS|NIPS|ICLR|ICML)(?![A-Za-z])\s*[-–—'’]?\s*" + _YEAR + r"(?!\d)", re.I), "venue_first"),
    (re.compile(r"(?<!\d)" + _YEAR + r"\s+(CVPR|ICCV|ECCV|NeurIPS|NIPS|ICLR|ICML)(?![A-Za-z])", re.I), "year_first"),
]

# 明确不代表"已录用"的说法
_NEGATIVE = re.compile(
    r"\b(submitted\s+to|under\s+review|under\s+submission|rejected|desk\s+reject"
    r"|to\s+appear\s+in\s+arxiv|preprint)\b",
    re.I,
)
_WORKSHOP = re.compile(r"\bworkshop\b", re.I)
_FINDINGS = re.compile(r"\bfindings\b", re.I)

# 会议名后面紧跟的"轨道名"。白名单里的不算异常，其余视为可疑（可能是 workshop）
_TRACK_OK = {
    "oral", "spotlight", "poster", "accepted", "camera", "ready", "main",
    "conference", "proceedings", "long", "short", "findings", "highlight",
    "demo", "track", "datasets", "benchmarks", "blogposts", "position",
}
_TRACK_RE = re.compile(r"^\s*[\(\-–—,:]?\s*([A-Z][A-Za-z0-9&\-]*(?:\s+(?:and|&)\s+[A-Z][A-Za-z0-9&\-]*)?)")


def _extract_track(text: str, end: int) -> str | None:
    """取会议名+年份之后紧跟的那个词，用来判断是不是主会轨道。"""
    tail = text[end:end + 60]
    m = _TRACK_RE.match(tail)
    if not m:
        return None
    word = m.group(1).strip()
    if word.lower() in _TRACK_OK:
        return None
    return word


def _norm_year(y: str) -> int | None:
    y = y.strip()
    if y.startswith("'"):
        return 2000 + int(y[1:])
    try:
        v = int(y)
        return v if 2000 <= v <= 2100 else None
    except ValueError:
        return None


def detect_venue(comment: str | None, journal_ref: str | None) -> dict:
    """
    从 comment / journal_ref 里识别「这篇论文被哪个会议接收了」。

    返回：
        {venue_key, venue_year, confidence, evidence, status, is_workshop, is_findings}
        status ∈ accepted / workshop / findings / unknown
    """
    text = " ".join(x for x in (comment, journal_ref) if x).strip()
    out = {
        "venue_key": None,
        "venue_year": None,
        "confidence": None,
        "evidence": None,
        "status": "unknown",
        "track": None,
        "needs_verify": False,
        "is_workshop": False,
        "is_findings": False,
    }
    if not text or not _ANY_VENUE.search(text):
        return out

    is_ws = bool(_WORKSHOP.search(text))
    is_findings = bool(_FINDINGS.search(text))
    negative = bool(_NEGATIVE.search(text))

    # 1) 会议 + 年份
    for pat, order in _VENUE_YEAR_PATTERNS:
        for m in pat.finditer(text):
            g = m.groups()
            raw_venue, raw_year = (g[0], g[1]) if order == "venue_first" else (g[1], g[0])
            key = _ALIAS_INDEX.get(raw_venue.lower())
            if not key:
                continue
            year = _norm_year(raw_year)
            if not year:
                continue
            # 出现 "submitted to" / "under review" 等字样 → 不是录用，只记候选
            status = "submitted" if negative else ("workshop" if is_ws else ("findings" if is_findings else "accepted"))
            track = _extract_track(text, m.end())
            # 会议名后有非主会轨道词（如 "NeurIPS 2026 Interp4Discovery"）→ 很可能 workshop
            suspect = bool(track) and not negative
            conf = "low" if negative else ("medium" if suspect else "high")
            if suspect and not is_ws:
                status = "workshop_suspect"
            out.update(
                venue_key=key,
                venue_year=year,
                evidence=m.group(0).strip(),
                confidence=conf,
                status=status,
                track=track,
                needs_verify=suspect,
                is_workshop=is_ws or suspect,
                is_findings=is_findings,
            )
            return out

    # 2) 只有会议名没有年份 —— 年份取文本里第一个四位年，置信度降级
    m = _ANY_VENUE.search(text)
    if m:
        raw = m.group(0)
        key = _ALIAS_INDEX.get(raw.lower())
        if key:
            ym = re.search(r"20\d{2}", text)
            # journal_ref 是正式出版信息，比"随便提一嘴"可信
            conf = "medium" if (journal_ref and ym and not negative) else "low"
            status = "submitted" if negative else ("workshop" if is_ws else ("findings" if is_findings else "accepted"))
            out.update(
                venue_key=key,
                venue_year=int(ym.group(0)) if ym else None,
                evidence=raw,
                confidence=conf,
                status=status,
                is_workshop=is_ws,
                is_findings=is_findings,
            )
    return out


# ---------------------------------------------------------------------------
# CV 相关性预筛（启发式，只做"要不要送 LLM"的分流，不做最终判定）
# ---------------------------------------------------------------------------
CV_STRONG = [
    r"\bimage[s]?\b", r"\bvideo[s]?\b", r"\bvisual\b", r"\bvision\b", r"\bvision-language\b",
    r"\bsegmentation\b", r"\bobject detection\b", r"\bdetector\b", r"\bpoint cloud\b",
    r"\b3d (scene|reconstruction|generation)\b", r"\bnerf\b", r"\bgaussian splatting\b",
    r"\bdiffusion model\b", r"\bimage generation\b", r"\btext-to-image\b", r"\bimage-to-image\b",
    r"\boptical flow\b", r"\bdepth estimation\b", r"\bpose estimation\b", r"\bslam\b",
    r"\bface\b", r"\brecognition\b", r"\bsegmentation\b", r"\bmedical image\b",
    r"\bremote sensing\b", r"\bautonomous driving\b", r"\bsuper-resolution\b", r"\bdenoising\b",
    r"\bmultimodal\b", r"\bvlm\b", r"\bvision transformer\b", r"\bvit\b",
]
CV_WEAK = [
    r"\bgenerative model\b", r"\bdiffusion\b", r"\bself-supervised\b", r"\bcontrastive\b",
    r"\brepresentation learning\b", r"\btransfer\b", r"\bdomain adaptation\b",
    r"\bgraph neural\b", r"\battention\b", r"\btransformer\b", r"\bembeddings?\b",
    r"\b3d\b", r"\bgeometry\b", r"\breconstruction\b", r"\bcompression\b",
]
_RE_STRONG = [re.compile(p, re.I) for p in CV_STRONG]
_RE_WEAK = [re.compile(p, re.I) for p in CV_WEAK]


def cv_hint(title: str, abstract: str, categories: list[str]) -> tuple[str, float]:
    """返回 (core/likely/weak, score)。cs.CV 直接 core。"""
    text = f"{title} {abstract}"
    strong = sum(1 for p in _RE_STRONG if p.search(text))
    weak = sum(1 for p in _RE_WEAK if p.search(text))

    if "cs.CV" in categories:
        return "core", 1.0
    if strong >= 3:
        return "core", 0.9
    if strong >= 1:
        return "likely", 0.6
    if weak >= 3:
        return "likely", 0.4
    return "weak", 0.1


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def build_query(preset: str, extra: str | None) -> str:
    presets = {
        # 视觉为主干
        "cv": "cat:cs.CV",
        # 视觉 + 方法外溢来源（ML/多模态/机器人），量大约 4~5 倍
        "wide": " OR ".join(
            f"cat:{c}" for c in ["cs.CV", "cs.LG", "cs.AI", "cs.MM", "cs.RO", "cs.CL"]
        ),
        # 只抓 ML 三大会常见投稿分类
        "ml": " OR ".join(f"cat:{c}" for c in ["cs.LG", "cs.AI", "stat.ML"]),
    }
    q = presets.get(preset, preset)  # 允许直接传 search_query
    if extra:
        q = f"({q}) AND ({extra})"
    return q


def fetch_page(query: str, start: int, page_size: int, retries: int = 4) -> ET.Element | None:
    params = {
        "search_query": query,
        "start": start,
        "max_results": page_size,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(API, params=params, timeout=TIMEOUT, headers={"User-Agent": UA})
            if r.status_code == 200:
                return ET.fromstring(r.content)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 5 * attempt
                console.print(f"  [yellow]HTTP {r.status_code}，{wait}s 后重试 ({attempt}/{retries})[/]")
                time.sleep(wait)
                continue
            console.print(f"  [red]HTTP {r.status_code}：{r.text[:200]}[/]")
            return None
        except (requests.RequestException, ET.ParseError) as e:
            wait = 5 * attempt
            console.print(f"  [yellow]{type(e).__name__}，{wait}s 后重试 ({attempt}/{retries})[/]")
            time.sleep(wait)
    return None


def parse_entry(entry: ET.Element) -> dict | None:
    def t(tag: str, ns: str = "atom") -> str:
        el = entry.find(f"{ns}:{tag}", NS)
        return (el.text or "").strip() if el is not None and el.text else ""

    raw_id = t("id")
    m = re.search(r"abs/([^v]+)(v\d+)?$", raw_id)
    if not m:
        return None
    arxiv_id = m.group(1)
    version = int((m.group(2) or "v1").lstrip("v"))

    title = re.sub(r"\s+", " ", t("title"))
    abstract = re.sub(r"\s+", " ", t("summary"))
    authors = [
        (a.find("atom:name", NS).text or "").strip()
        for a in entry.findall("atom:author", NS)
        if a.find("atom:name", NS) is not None
    ]
    categories = [c.get("term") for c in entry.findall("atom:category", NS) if c.get("term")]
    primary = entry.find("arxiv:primary_category", NS)
    primary_cat = primary.get("term") if primary is not None else (categories[0] if categories else "")

    comment_el = entry.find("arxiv:comment", NS)
    comment = re.sub(r"\s+", " ", (comment_el.text or "").strip()) if comment_el is not None else ""
    jr_el = entry.find("arxiv:journal_ref", NS)
    journal_ref = re.sub(r"\s+", " ", (jr_el.text or "").strip()) if jr_el is not None else ""
    doi_el = entry.find("arxiv:doi", NS)
    doi = (doi_el.text or "").strip() if doi_el is not None else ""

    published, updated = t("published"), t("updated")
    venue = detect_venue(comment, journal_ref)
    hint, score = cv_hint(title, abstract, categories)

    return {
        "arxiv_id": arxiv_id,
        "version": version,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "primary_category": primary_cat,
        "categories": categories,
        "published": published,
        "updated": updated,
        "comment": comment,
        "journal_ref": journal_ref,
        "doi": doi,
        "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        **venue,
        "cv_hint": hint,
        "cv_score": score,
        "title_fp": title_fingerprint(title),
    }


def title_fingerprint(title: str) -> str:
    """标题指纹：小写 + 去标点 + 去空白 → sha1 前 16 位。跨源去重用。"""
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", title.lower())
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def collect(query: str, days: int, max_papers: int | None, page_size: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    papers: list[dict] = []
    start = 0
    total = None

    while True:
        root = fetch_page(query, start, page_size)
        if root is None:
            console.print("  [red]拉取失败，中止[/]")
            break
        if total is None:
            tr = root.find("os:totalResults", NS)
            total = int(tr.text) if tr is not None and tr.text else 0
            console.print(f"  查询命中总数：{total:,}")

        entries = root.findall("atom:entry", NS)
        if not entries:
            break

        stop = False
        for e in entries:
            p = parse_entry(e)
            if not p:
                continue
            try:
                pub = datetime.fromisoformat(p["published"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if pub < cutoff:
                stop = True
                continue
            papers.append(p)
            if max_papers and len(papers) >= max_papers:
                stop = True
                break

        console.print(
            f"  第 {start // page_size + 1} 页：累计 {len(papers)} 篇"
            f"（最早 {papers[-1]['published'][:10] if papers else '-'}）"
        )
        if stop or len(entries) < page_size:
            break
        start += page_size
        time.sleep(REQUEST_INTERVAL)

    return papers


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    arxiv_id        TEXT PRIMARY KEY,
    version         INTEGER,
    title           TEXT NOT NULL,
    abstract        TEXT,
    authors         TEXT,          -- JSON array
    primary_category TEXT,
    categories      TEXT,          -- JSON array
    published       TEXT,
    updated         TEXT,
    comment         TEXT,
    journal_ref     TEXT,
    doi             TEXT,
    abs_url         TEXT,
    pdf_url         TEXT,
    venue_key       TEXT,
    venue_year      INTEGER,
    venue_confidence TEXT,
    venue_evidence  TEXT,
    venue_status    TEXT,
    track           TEXT,
    needs_verify    INTEGER DEFAULT 0,
    is_workshop     INTEGER DEFAULT 0,
    is_findings     INTEGER DEFAULT 0,
    cv_hint         TEXT,
    cv_score        REAL,
    title_fp        TEXT,
    first_seen      TEXT,
    last_seen       TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_venue ON papers(venue_key, venue_year);
CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published);
CREATE INDEX IF NOT EXISTS idx_papers_fp ON papers(title_fp);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    query       TEXT,
    days        INTEGER,
    fetched     INTEGER,
    new         INTEGER,
    upgraded    INTEGER,
    snapshot    TEXT
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert(conn: sqlite3.Connection, p: dict, now: str) -> tuple[str, list[str]]:
    """返回 (new|upgraded|dup, 升级的字段列表)"""
    row = conn.execute(
        "SELECT venue_key, venue_year, version, comment FROM papers WHERE arxiv_id = ?",
        (p["arxiv_id"],),
    ).fetchone()

    if row is None:
        conn.execute(
            """INSERT INTO papers (arxiv_id, version, title, abstract, authors, primary_category,
                categories, published, updated, comment, journal_ref, doi, abs_url, pdf_url,
                venue_key, venue_year, venue_confidence, venue_evidence, venue_status,
                track, needs_verify, is_workshop, is_findings, cv_hint, cv_score, title_fp,
                first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p["arxiv_id"], p["version"], p["title"], p["abstract"],
                json.dumps(p["authors"], ensure_ascii=False), p["primary_category"],
                json.dumps(p["categories"], ensure_ascii=False), p["published"], p["updated"],
                p["comment"], p["journal_ref"], p["doi"], p["abs_url"], p["pdf_url"],
                p["venue_key"], p["venue_year"], p["confidence"], p["evidence"], p["status"],
                p["track"], int(p["needs_verify"]), int(p["is_workshop"]), int(p["is_findings"]),
                p["cv_hint"], p["cv_score"], p["title_fp"], now, now,
            ),
        )
        return "new", []

    # 已存在：只在"信息变多"时更新（典型的场景是作者后来补了 Accepted to XXX）
    # upgrades 里直接存数据库列名，避免键名与列名不一致
    upgrades: list[str] = []
    old_venue, old_year, old_ver, old_comment = row
    if p["venue_key"] and not old_venue:
        upgrades += ["venue_key", "venue_year", "venue_confidence", "venue_evidence",
                     "venue_status", "track", "needs_verify"]
    elif p["venue_key"] == old_venue and p["venue_year"] and p["venue_year"] != old_year:
        upgrades += ["venue_year"]
    if p["version"] > (old_ver or 0):
        upgrades.append("version")
    if p["comment"] != (old_comment or ""):
        upgrades.append("comment")

    sets = ["last_seen = ?"]
    vals: list = [now]
    mapping = {
        "venue_key": p["venue_key"], "venue_year": p["venue_year"],
        "venue_confidence": p["confidence"], "venue_evidence": p["evidence"],
        "venue_status": p["status"],
        "track": p["track"], "needs_verify": int(p["needs_verify"]),
        "version": p["version"], "comment": p["comment"],
    }
    for f in upgrades:
        sets.append(f"{f} = ?")
        vals.append(mapping[f])
    if p["venue_key"]:
        sets += ["is_workshop = ?", "is_findings = ?"]
        vals += [int(p["is_workshop"]), int(p["is_findings"])]
    vals.append(p["arxiv_id"])
    conn.execute(f"UPDATE papers SET {', '.join(sets)} WHERE arxiv_id = ?", vals)

    if "venue_key" in upgrades:
        return "upgraded", upgrades
    return "dup", []


# ---------------------------------------------------------------------------
# 导出（给仓库 / 建站用）
# ---------------------------------------------------------------------------
EXPORT_DIR = DATA_DIR / "papers"
_EXPORT_FIELDS = [
    "arxiv_id", "version", "title", "abstract", "authors", "primary_category", "categories",
    "published", "updated", "comment", "journal_ref", "doi", "abs_url", "pdf_url",
    "venue_key", "venue_year", "confidence", "evidence", "status", "track",
    "needs_verify", "is_workshop", "is_findings", "cv_hint", "cv_score", "title_fp",
]


def export_monthly(papers: list[dict], out_dir: Path = EXPORT_DIR) -> list[Path]:
    """
    按月分片导出 JSONL：data/papers/2026-11.jsonl

    为什么不用 SQLite 进仓库：二进制文件每次提交都是整份重写，diff 不可读、
    仓库会迅速膨胀。JSONL 是文本，diff 只显示新增行，也方便建站时直接读。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    touched: list[Path] = []
    by_month: dict[str, list[dict]] = {}
    for p in papers:
        month = (p["published"] or "")[:7] or "unknown"
        by_month.setdefault(month, []).append(p)

    for month, items in by_month.items():
        path = out_dir / f"{month}.jsonl"
        merged: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                merged[rec["arxiv_id"]] = rec
        for p in items:
            rec = {k: p[k] for k in _EXPORT_FIELDS}
            merged[p["arxiv_id"]] = rec
        rows = sorted(merged.values(), key=lambda r: (r.get("published") or ""), reverse=True)
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        touched.append(path)
    return touched


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
VENUE_LABEL = {
    "cvpr": "CVPR", "iccv": "ICCV", "eccv": "ECCV",
    "neurips": "NeurIPS", "iclr": "ICLR", "icml": "ICML",
}


def print_report(papers: list[dict], stats: dict, snapshot: Path) -> None:
    by_venue: dict[str, int] = {}
    by_hint: dict[str, int] = {}
    for p in papers:
        if p["venue_key"] and p["status"] == "accepted":
            by_venue[p["venue_key"]] = by_venue.get(p["venue_key"], 0) + 1
        by_hint[p["cv_hint"]] = by_hint.get(p["cv_hint"], 0) + 1

    console.print()
    console.print("[bold]采集结果[/]")
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("抓取", f"{stats['fetched']} 篇")
    t.add_row("新增", f"[green]{stats['new']}[/] 篇")
    t.add_row("升级", f"[cyan]{stats['upgraded']}[/] 篇（后来补充了录用信息）")
    t.add_row("重复", f"{stats['dup']} 篇")
    t.add_row("耗时", f"{stats['elapsed']:.1f}s")
    t.add_row("快照", str(snapshot.relative_to(ROOT)))
    console.print(t)

    if by_venue:
        console.print("\n[bold]已识别录用（按会议）[/]")
        vt = Table(box=None, padding=(0, 2))
        vt.add_column("会议"); vt.add_column("篇数", justify="right")
        for k, v in sorted(by_venue.items(), key=lambda x: -x[1]):
            vt.add_row(VENUE_LABEL.get(k, k), str(v))
        console.print(vt)

    console.print(
        f"\n[bold]CV 相关度预筛[/]  core={by_hint.get('core', 0)}  "
        f"likely={by_hint.get('likely', 0)}  weak={by_hint.get('weak', 0)}"
    )
    if stats["upgrades"]:
        console.print("\n[bold cyan]本次升级的论文[/]")
        for u in stats["upgrades"][:10]:
            console.print(f"  · {u}")


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="arXiv 增量采集（线 B）")
    ap.add_argument("--preset", default="cv", help="cv / wide / ml，或直接给 search_query")
    ap.add_argument("--extra", default=None, help="附加过滤条件，如 'abs:diffusion'")
    ap.add_argument("--days", type=int, default=3, help="回溯天数（默认 3）")
    ap.add_argument("--max", type=int, default=None, help="最多取多少篇")
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--only-accepted", action="store_true", help="只输出已识别录用的论文")
    ap.add_argument("--no-export", action="store_true", help="不写 data/papers/*.jsonl")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    query = build_query(args.preset, args.extra)
    console.rule("[bold]arXiv 增量采集")
    console.print(f"查询：{query}")
    console.print(f"时间窗：最近 {args.days} 天")

    t0 = time.time()
    papers = collect(query, args.days, args.max, args.page_size)
    if not papers:
        console.print("[yellow]时间窗内没有新论文。[/]")
        return 0

    # 原始快照
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot = RAW_DIR / f"arxiv_{stamp}.jsonl"
    with snapshot.open("w", encoding="utf-8") as f:
        for p in papers:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    conn = sqlite3.connect(args.db)
    init_db(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = {"fetched": len(papers), "new": 0, "upgraded": 0, "dup": 0, "upgrades": []}
    for p in papers:
        kind, fields = upsert(conn, p, now)
        if kind == "new":
            stats["new"] += 1
        elif kind == "upgraded":
            stats["upgraded"] += 1
            stats["upgrades"].append(f"{p['arxiv_id']} → {p['venue_key']} {p['venue_year']} ({p['evidence']})")
        else:
            stats["dup"] += 1
    conn.execute(
        "INSERT INTO runs (started_at, finished_at, query, days, fetched, new, upgraded, snapshot)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="seconds"),
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         query, args.days, stats["fetched"], stats["new"], stats["upgraded"], snapshot.name),
    )
    conn.commit()
    stats["elapsed"] = time.time() - t0

    print_report(papers, stats, snapshot)

    if not args.no_export:
        files = export_monthly(papers)
        for f in files:
            n = sum(1 for _ in f.open(encoding="utf-8"))
            console.print(
                f"[bold]导出[/] {f.relative_to(ROOT)}  "
                f"{n} 篇  {f.stat().st_size / 1024:.0f} KB"
            )

    if args.only_accepted:
        acc = [p for p in papers if p["venue_key"] and p["status"] == "accepted"]
        out = RAW_DIR / f"accepted_{stamp}.jsonl"
        with out.open("w", encoding="utf-8") as f:
            for p in acc:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        console.print(f"\n仅录用：{len(acc)} 篇 → {out.relative_to(ROOT)}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

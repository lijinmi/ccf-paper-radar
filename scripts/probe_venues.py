"""探测 OpenAlex 中各顶会的 source ID 与论文量。

用法：.venv\\Scripts\\python.exe scripts\\probe_venues.py

注意：OpenAlex 无 API key 时，整个 IP 共享每天 $0.10 额度（约 100 次请求）。
建议先申请免费 key：POST https://api.openalex.org/api-keys   body: {"email": "你的邮箱"}
然后把 key 写进 .env 的 OPENALEX_KEY。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API = "https://api.openalex.org/sources"
KEY = os.getenv("OPENALEX_KEY", "").strip()
MAILTO = os.getenv("CONTACT_EMAIL", "").strip()

# 每个会议给 2~3 个候选搜索词，命中率更高
TARGETS = {
    "CVPR": ["IEEE Conference on Computer Vision and Pattern Recognition"],
    "ICCV": ["International Conference on Computer Vision"],
    "ECCV": ["European Conference on Computer Vision"],
    "NeurIPS": ["Neural Information Processing Systems", "Advances in Neural Information Processing"],
    "ICML": ["International Conference on Machine Learning"],
    "ICLR": ["International Conference on Learning Representations"],
    "ACL": ["Annual Meeting of the Association for Computational Linguistics"],
    "EMNLP": ["Empirical Methods in Natural Language Processing"],
    "NAACL": ["North American Chapter of the Association for Computational Linguistics"],
    "AAAI": ["AAAI Conference on Artificial Intelligence"],
    "IJCAI": ["International Joint Conference on Artificial Intelligence"],
}


def query(term: str, n: int = 3) -> list[dict]:
    params = {"search": term, "per-page": n}
    if KEY:
        params["api_key"] = KEY
    elif MAILTO:
        params["mailto"] = MAILTO
    r = requests.get(API, params=params, timeout=40)
    r.raise_for_status()
    out = []
    for s in r.json().get("results", []):
        out.append(
            {
                "id": s.get("id", "").rsplit("/", 1)[-1],
                "name": s.get("display_name"),
                "type": s.get("type"),
                "works": s.get("works_count"),
                "issn": (s.get("issn") or [None])[0],
            }
        )
    return out


def main() -> int:
    if not KEY:
        print("⚠ 未配置 OPENALEX_KEY，将使用共享的每日 $0.10 额度（约 100 次请求）\n")

    result: dict[str, list[dict]] = {}
    for key, terms in TARGETS.items():
        hits: list[dict] = []
        for t in terms:
            try:
                hits = query(t)
            except Exception as exc:  # noqa: BLE001
                print(f"{key:<8} 请求失败: {exc}")
                hits = []
            if hits:
                break
            time.sleep(1.0)
        result[key] = hits
        print(f"\n=== {key} ===")
        if not hits:
            print("  (无结果)")
        for h in hits:
            print(f"  {h['id']:<14} works={h['works']:<8} {h['type']:<10} {h['name']}")
        time.sleep(0.6)

    out = ROOT / "data" / "probe_venues.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

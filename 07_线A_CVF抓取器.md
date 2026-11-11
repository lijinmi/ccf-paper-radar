# 线 A：CVF Open Access 抓取器（已跑通）

> 2026-11-11 · `scripts/collect_cvf.py`
> 结果：**CVPR 2026 全量 4,042 篇，摘要 100% 抓齐，零失败，25 分钟**

---

## 一、为什么要单独写一个

线 B（arXiv）只能捞到「正在放榜」的会议。CVPR 2026 是 6 月开的会，那批论文半年前就发过 arXiv 了，你在今天的新投稿里一篇都看不到。

CVPR/ICCV 的全部论文只在 **CVF Open Access** 上。所以线 A 不是线 B 的备选，是**主力**——它决定了站里 CV 论文的 70%。

## 二、实测数字

| 阶段 | 数据 |
|---|---|
| 列表页 | 12.1 MB（原始 HTML），**2 分 01 秒**下完 |
| 解析 | **4,042 篇**，0 遗漏、0 重复 |
| 详情页 | 4,042 个，6 并发，**24 分 56 秒**，2.7 篇/s，**成功率 100%** |
| 导出 | `data/papers/cvpr-2026.jsonl`，8.6 MB（2.2 KB/篇） |
| 磁盘 | 缓存 12 MB + 数据库 29 MB + 导出 8.6 MB |

质量抽查：

```
摘要长度 中位数 1378 字符（最短 562，最长 2770）
摘要 <200 字符：0 篇
标题重复：0      指纹重复：0
作者为空：0      页码缺失：0
HTML 实体残留：0
```

**官方录用 4,090 篇，CVF 上实际 4,042 篇，差 48 篇（1.2%）。** 这不是抓漏——列表页里的 `paper.html` 链接去重后正好 4,042 个，是 CVF 本身还没收全（撤稿或尚未上传）。

## 三、CVF 的三个坑（都是实测踩出来的）

**1. 分页参数彻底失效**

```
https://openaccess.thecvf.com/CVPR2026?day=0   → HTTP 500
https://openaccess.thecvf.com/CVPR2026?day=1   → HTTP 500
https://openaccess.thecvf.com/CVPR2026?day=all → HTTP 200（12 MB 单页）
```

老教程里教的「按天分页抓」在 2026 已经不能用了。你只能拿那 12 MB 的单页。

**2. 不带 gzip 会慢 10 倍**

CVF 响应头有 `Vary: Accept-Encoding`，说明支持压缩。实测对比：

| | 裸 curl | `--compressed` |
|---|---|---|
| 30 秒下载量 | 约 270 篇 | **899 篇** |
| 全量耗时 | 约 20 分钟 | **2 分钟** |

12 MB 压缩后传输量只有约 2.5 MB。`requests` 默认就会发 `Accept-Encoding: gzip`，所以脚本里没问题——但**如果你用 curl 调试，一定要加 `--compressed`**，否则会以为这站不可用。

**3. 不支持 Range，不能断点续传**

```
curl -r 0-999 → HTTP 200（返回全量，不是 206）
```

意味着下载中断只能整份重下。所以列表页**必须落盘缓存**，脚本里判断「大于 1 MB 且结尾有 `</html>`」才算完整，否则重下。

## 四、为什么设计成两阶段

因为列表页和详情页的速度差了三个数量级：

- 列表页：12 MB / 2 分钟 ≈ 100 KB/s
- 详情页：7 KB / 2.3 秒 ≈ 3 KB/s（主要是首字节延迟）

如果把摘要也塞进列表页一次拿完，你没法中断——断了就丢 25 分钟。所以拆成：

```
list     下载 + 解析 → data/cache/cvf/CVPR2026.html + SQLite 索引
abstract 6 线程并发补摘要 → 写 SQLite，进度可中断、重跑自动跳过已抓的
export   → data/papers/cvpr-2026.jsonl
```

三个阶段可以单独重跑。`--limit 100` 先试水，确认没问题再全量。

## 五、数据结构

CVF 的每条记录比 arXiv 多几个有用字段：

| 字段 | 说明 |
|---|---|
| `detail_url` | 主键，论文详情页 |
| `bibkey` | `Kumar_2026_CVPR`，写引用时直接用 |
| `pages` | `19096-19106`，正式论文集页码 |
| `pdf_url` | 直链 |
| `title_fp` | 标题指纹，和 arXiv 同算法，用于跨源去重 |
| `arxiv_id` | 按指纹关联到的 arXiv ID（可为 null） |

**关于 arXiv 关联：当前是 0 篇。** 这不是 bug——arXiv 分片里只有最近 3 天的 256 篇，而 CVPR 2026 的论文是半年前发的。等你把 arXiv 的回溯跑起来（或者做历史补齐），这个关联率会上去，一篇论文就能同时有「会议正式版」和「预印本」两个链接。

## 六、怎么跑

本地：

```bash
# 全流程（约 27 分钟，其中 25 分钟在补摘要）
./.venv/Scripts/python.exe scripts/collect_cvf.py --conf CVPR2026

# 或者分阶段
./.venv/Scripts/python.exe scripts/collect_cvf.py --conf CVPR2026 --phase list
./.venv/Scripts/python.exe scripts/collect_cvf.py --conf CVPR2026 --phase abstract --limit 100
./.venv/Scripts/python.exe scripts/collect_cvf.py --conf CVPR2026 --phase export
```

云端：`.github/workflows/collect-cvf.yml`，手动触发，填 `CVPR2026`。CVF 一年只更新几次，不需要定时。

## 七、还缺哪几个会

2026 年的线 A 只完成了三分之一：

| 会议 | 源 | 状态 |
|---|---|---|
| CVPR 2026 | CVF | ✅ 4,042 篇已入库 |
| ECCV 2026 | ECVA（`ecva.net/papers.php`） | ❌ 本机不通，结构也不同，要另写解析器 |
| ICLR 2026 | OpenReview | ❌ Cloudflare 挑战，API 403 |
| ICML 2026 | PMLR | ❌ 需建「年份 → 卷号」映射 |
| ICCV 2026 | — | **不存在**（奇数年） |

优先级：**PMLR（ICML）> ECVA（ECCV）> OpenReview（ICLR）**。PMLR 结构最简单（`proceedings.mlr.press/v235/` 这种卷号页，一页就是全部论文），而且本机能连（约 20 秒）。OpenReview 最难，建议最后做，或者接受「一年手动导一次」。

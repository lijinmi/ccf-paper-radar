# 05 · GitHub Actions 与 Cloudflare Pages 上手指南

> 2026-11-11 ｜ 面向：没用过 CI/CD 的人
> 读完你应该能做到：代码推到 GitHub，服务器定时帮你抓论文，网站自动更新

---

## 1. 三个东西，一句话说清

| 名字 | 是什么 | 类比 | 在你项目里的角色 |
|---|---|---|---|
| **GitHub** | 代码托管平台 | 带版本历史的网盘 | 存代码和数据，是所有自动化的起点 |
| **GitHub Actions** | GitHub 附赠的**云端虚拟机**，可以按时间或事件自动跑命令 | 一台不要钱、按时上下班的电脑 | **抓论文、调 LLM 翻译**（它在美国，正好绕开你连不上的站） |
| **Cloudflare Pages** | 静态网站托管 + 全球 CDN | 免费的世界各地机房帮你发网页 | **把生成好的 HTML 放上去**，别人就能访问 |

一句话串起来：

> **Actions 干活（抓+翻），GitHub 存结果，Pages 展示结果。**

你本机只负责写代码和调试，剩下的交给它们，而且**全免费**。

---

## 2. 为什么这套组合正好适合你这个项目

不是我随便选的，是三个现实逼出来的：

| 你的现实 | 这套方案怎么解决 |
|---|---|
| ECVA / NeurIPS / OpenReview 从大陆连不上（实测超时和 403） | Actions 的 runner 在美国 Azure 机房，直接访问没问题 |
| CVF 只有 11 KB/s，抓全量要十几分钟 | 机房带宽是百兆级，几十秒搞定 |
| 不想买服务器、不想运维、不想被攻击 | 没有服务器。Pages 是纯静态托管，无后端可攻击 |
| 预算有限 | Actions（公开仓库）不限分钟数，Pages 不限流量不限请求，**¥0/月** |

---

## 3. 整体链路

```
  你的电脑                      GitHub                         Cloudflare
┌──────────┐   git push   ┌──────────────┐  定时触发   ┌────────────────┐
│ 写代码    │ ───────────> │  代码仓库     │ ─────────> │ Actions 虚拟机  │
│ 本地调试  │              │  (main 分支)  │            │ 1. 抓论文       │
└──────────┘              │              │            │ 2. 分类+翻译    │
                          │              │ <───────── │ 3. commit 数据  │
                          │  data/*.json │   git push └────────────────┘
                          │              │
                          │              │  push 事件
                          │              │ ─────────> ┌────────────────┐
                          └──────────────┘            │ Pages 自动构建  │
                                                      │ npm run build  │
                                                      │ → 全球 CDN     │
                                                      └────────────────┘
                                                              │
                                                      https://你的站.pages.dev
```

关键点：**你不需要手动部署**。数据一提交，Pages 检测到变化就自动重建，1~2 分钟后线上就是新的。

---

## 4. 第一步：把项目变成 Git 仓库（5 分钟）

你机器上已经有 `git 2.55` 和 `node 22.22`，不用装。

```bash
cd D:/WB/CCF_web

# 初始化仓库
git init
git add .
git commit -m "init: 项目骨架 + 方案文档"

# 关联远程仓库（先在 github.com 上手动 New repository，名字如 ccf-paper-radar）
git remote add origin https://github.com/你的用户名/ccf-paper-radar.git
git branch -M main
git push -u origin main
```

**建仓库时选 Public**。原因：公开仓库的 Actions 分钟数**完全免费不限**，私有仓库每月只有 2000 分钟。你的代码不值钱，数据才值钱。

⚠️ 推之前确认 `.gitignore` 已排除 `.env`（已在第 00 篇配好）。API key 绝不能进仓库。

**如果 `git push` 卡住或很慢**（国内常见）：

```bash
# 方案一：改用 SSH over 443 端口（绕过 22 端口封锁）
# ~/.ssh/config 里加：
#   Host github.com
#     HostName ssh.github.com
#     Port 443
#     User git

# 方案二：只对 GitHub 走代理
git config --global http.https://github.com.proxy http://127.0.0.1:7890
```

---

## 5. GitHub Actions 的 6 个概念

只要理解这 6 个词，Actions 就没有神秘感了：

| 概念 | 含义 | 在你的项目里 |
|---|---|---|
| **workflow** | 一个 YAML 文件，描述"要自动做什么" | `.github/workflows/daily.yml` |
| **trigger（on）** | 什么时候跑 | 每天凌晨 / 手动点按钮 / 代码推送时 |
| **job** | 一次运行里的一个任务，跑在一台虚拟机上 | "抓论文"是一个 job |
| **step** | job 里的一个步骤 | "pip install" 是一步，"跑脚本"是一步 |
| **runner** | 执行 job 的虚拟机 | `ubuntu-latest`，在美国 |
| **secrets** | 加密的环境变量 | 你的 API key 存在这里，日志里自动打码 |

**关键认知**：workflow 文件必须放在 `.github/workflows/` 目录下，文件名任意，后缀 `.yml`。

---

## 6. 第二步：写一个真能跑的 workflow

新建 `.github/workflows/daily.yml`：

```yaml
name: 每日论文增量

on:
  # 定时触发：UTC 18:00 = 北京时间次日 02:00
  schedule:
    - cron: "0 18 * * *"
  # 允许在网页上手动点 "Run workflow"
  workflow_dispatch:

# 需要写权限，才能把抓到的数据 commit 回仓库
permissions:
  contents: write

jobs:
  ingest:
    runs-on: ubuntu-latest
    timeout-minutes: 60

    steps:
      - name: 拉取代码
        uses: actions/checkout@v4

      - name: 装 Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.13"
          cache: pip

      - name: 装依赖
        run: pip install -r requirements.txt

      - name: 抓取（arXiv 增量）
        env:
          OPENALEX_KEY: ${{ secrets.OPENALEX_KEY }}
        run: python scripts/collect_arxiv.py --days 1

      - name: 分类 + 判相关性
        env:
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
          LLM_BASE_URL: ${{ secrets.LLM_BASE_URL }}
        run: python scripts/classify.py --pending

      - name: 翻译摘要
        env:
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
          LLM_BASE_URL: ${{ secrets.LLM_BASE_URL }}
        run: python scripts/translate.py --pending

      - name: 提交数据回仓库
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/
          if git diff --staged --quiet; then
            echo "没有新数据，跳过"
          else
            git commit -m "chore(data): 每日增量 [skip ci]"
            git push
          fi
```

几个必须知道的细节：

| 细节 | 说明 |
|---|---|
| **cron 是 UTC** | 北京时间 = UTC + 8。想每天早上 8 点跑，就写 `0 0 * * *` |
| **定时任务会延迟** | 高峰时段可能晚 15~30 分钟，别指望精确到分 |
| **60 天不活动会被停用** | 仓库 60 天没有任何提交，schedule 会被自动禁用，需要手动重新启用 |
| **`[skip ci]`** | 防止 bot 的提交再次触发 workflow，形成死循环 |
| **单个 job 上限 6 小时** | 全量抓取要拆成多个 job 或分片，别想一次跑完 |

---

## 7. 第三步：配 Secrets（存 API key）

网页路径：仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Name | 值 | 用途 |
|---|---|---|
| `OPENALEX_KEY` | 在 `openalex.org/settings/api` 申请 | OpenAlex 额度从 $0.10/天 提到 $1/天 |
| `LLM_API_KEY` | DeepSeek / 通义 / 智谱 的 key | 翻译与分类 |
| `LLM_BASE_URL` | 例如 `https://api.deepseek.com/v1` | OpenAI 兼容接口地址 |

**Secrets 的特性**：
- 写进去就看不到了，只能覆盖，不能读取；
- 日志里出现会被自动替换成 `***`；
- 本地开发用 `.env`，线上用 Secrets，**代码里永远读环境变量，不写死**。

```python
import os
key = os.getenv("LLM_API_KEY")   # 正确
# key = "sk-xxxx"                # 错误，绝不能这样
```

---

## 8. 第四步：接上 Cloudflare Pages（10 分钟）

1. 打开 `dash.cloudflare.com` 注册（邮箱即可，不需要信用卡）。
2. 左侧 **Workers & Pages** → **Create** → **Pages** → **Connect to Git**。
3. 授权 GitHub，选中你的仓库，分支选 `main`。
4. 构建设置：

| 字段 | 填什么 |
|---|---|
| Framework preset | `Astro`（用 Next.js 就选 Next.js） |
| Build command | `npm run build` |
| Build output directory | `dist` |
| 环境变量 | `NODE_VERSION = 22` |

5. 点 **Save and Deploy**。等 1~3 分钟，拿到 `https://ccf-paper-radar.pages.dev`。

**之后每次 `git push` 到 main，Pages 都会自动重新构建部署。** 你什么都不用管。

### 绑自己的域名

Pages 项目 → **Custom domains** → **Set up a domain** → 输入你的域名 → 按提示加 CNAME 记录。
SSL 证书自动签发，几十秒生效。

---

## 9. 免费额度：够不够用

| 项目 | 免费额度 | 你的用量 | 结论 |
|---|---|---|---|
| Actions 分钟数 | 公开仓库**不限**；私有 2,000 分钟/月 | 每天 5 分钟 × 30 = 150 分钟 | 绰绰有余 |
| Actions 单 job 时长 | 6 小时 | 几分钟 | 够 |
| Pages 构建次数 | **500 次/月** | 每天 1 次 = 30 次 | 够 |
| Pages 带宽 / 请求 | **无限** | — | 永远够 |
| Pages 单站点文件数 | **约 20,000 个** | 1.67 万篇 → 卡边 | ⚠️ 见下 |
| Pages 单文件大小 | 25 MiB | 页面 20 KB | 够 |
| Pages 并发构建 | 1 个 | — | 够 |

⚠️ **两个需要盯的**：
1. **2 万文件上限**——我们算过，2026 年 CV 相关约 9,000 篇，加上列表页/标签页约 1.1 万个文件，安全。但如果明年扩到两年数据（1.67 万篇）就贴边了。到那时的解法：换 GitHub Pages（1 GB 上限看体积不看数量）或对象存储 + CDN。
2. **500 次构建/月**——别让 workflow 频繁 push 数据，否则一天构建十几次就爆了。每天 1~2 次是安全线。

---

## 10. 中国网络的三个坑

| 坑 | 表现 | 解法 |
|---|---|---|
| **push 慢/失败** | `git push` 卡在 writing objects | SSH over 443（见第 4 节），或配代理 |
| **Actions 日志打不开** | 点开运行记录一直转圈 | 日志走 `githubusercontent.com`，需要代理；本机看不到就等邮件通知结果 |
| **pages.dev 访问不稳** | 国内有时打不开 | 绑自定义域名会好一些；**但长期面向国内读者，最终还是要国内服务器 + 备案**（见 `02` 文档） |

最后这条要有心理准备：**Cloudflare Pages 适合"先做出来、给同行看"，不适合"给国内大众访问"**。这不是技术问题，是网络环境问题。

---

## 11. 如果 GitHub 实在用不了：三条替代路线

| 方案 | 做法 | 成本 | 适合 |
|---|---|---|---|
| **A. 本地跑 + 直传** | 本地每天手动跑脚本，`npx wrangler pages deploy dist` 直接传 Pages（不经过 GitHub） | ¥0 | 想完全掌控，能接受每天点一下 |
| **B. 香港轻量服务器** | 买 §40/月 的香港机，装 cron + nginx，自己跑抓取和构建 | ¥480/年 | 要完全自动化，又要海外网络 |
| **C. 国内服务器 + 备案** | 腾讯云轻量 ¥99/年起，ICP 备案后托管 | ¥99~576/年 | 主要读者在国内 |

方案 A 的命令：

```bash
npm install -g wrangler
wrangler login
wrangler pages deploy dist --project-name ccf-paper-radar
```

---

## 12. 30 分钟上手清单

- [ ] 注册 GitHub（如果还没有）
- [ ] 建 Public 仓库 `ccf-paper-radar`
- [ ] 本地 `git init` + `git push`（第 4 节命令）
- [ ] 建 `.github/workflows/daily.yml`（第 6 节，先只留"抓 arXiv"一步）
- [ ] 加 3 个 Secrets（第 7 节）
- [ ] 点 Actions 页面的 **Run workflow** 手动跑一次，看是不是绿的
- [ ] 注册 Cloudflare → Pages → 连仓库 → 部署
- [ ] 拿到 `xxx.pages.dev` 链接，发给朋友试试能不能打开

**别一上来就写完整 workflow。** 先让"checkout → 装 Python → 跑一个 print"这条最小链路变绿，再往里加步骤。绿过一次之后，后面全是复制粘贴。

---

## 13. 一页速查

```
GitHub        = 代码仓库（Public 仓库的 Actions 免费不限分钟）
Actions       = 云端虚拟机，定时跑脚本。文件放 .github/workflows/*.yml
Pages         = 免费静态托管 + CDN，连上仓库后 push 即自动部署

分工：Actions 负责"抓 + 翻"（它在海外，能访问 ECVA/NeurIPS/OpenReview）
      Pages  负责"展示"（构建 npm run build，产物目录 dist）
      两者通过"git push"这一个动作串起来

三个数字要记住：6 小时（单 job 上限）、500 次（Pages 月构建上限）、20000 个文件（Pages 单站上限）

cron 用 UTC：北京时间 = UTC + 8
```

配套阅读：`04_2026最新论文获取.md`（抓什么、从哪抓）

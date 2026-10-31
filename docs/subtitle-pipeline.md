# Android 字幕链路：四种实现与当前选择

> 本文记录手机端（Morphe YouTube + 本网关）AI 字幕的四种实现方式、各自的实测数据与优劣，
> 以及**现在线上运行的是哪一种**、怎么切换、怎么回滚。

---

## 0. 客户端协议长什么样（一切问题的起点）

Morphe 的 AI 字幕补丁（`RebuildProtocol`）不走「一次问一句」的简单协议，而是：

```
POST /v1/plain/chat/completions
{
  "model": "...",
  "response_format": {"type": "json_schema", "json_schema": {...}},
  "messages": [
    {"role": "system", "content": "<约 350 词的 PROMPT>"},
    {"role": "user",   "content": "{\"block\":\"b1_31_77\",\"language\":\"zh-Hans\",
                                   \"source_text\":\"...\",\"owned_tokens\":[[0,\"When\"],...],
                                   \"context_before\":\"...\",\"context_after\":\"...\", ...}"}
  ]
}
```

回复必须是：

```json
{"block":"b1_31_77","events":[{"source":"逐字引文","text":"译文"}, ...]}
```

三条硬约束，缺一不可：

1. `source` 必须是 `source_text` 的**逐字子串**；
2. 多条 `source` 必须**按顺序完整划分** `source_text`（不能漏字、不能多字）；
3. 不允许出现 `source_text` 之外的内容。

任何一条不满足，App 会**丢弃整块并重试** —— 用户看到的就是「翻译中」反复出现。

---

## 1. 四种实现

### A. 注入式逐块翻译（原桌面版思路，已废弃）

```
App 块 → 网关注入【语境参考】+【术语映射表】+【占位符保护】+「只输出译文」
       → hy7b → 原样回给 App
```

| 项 | 数据 |
|---|---|
| 单块耗时 | **1.5–1.9s**（prompt 实测 3619 token） |
| 首条可见 | 十几秒~几十秒 |
| 并发 3 之后 | 明显排队 |

**优点**：术语一致性好、专有名词可控（对桌面长文翻译有价值）。

**缺点**：
- 每次请求都付 3619 token 的 prefill，本地 7B 单卡 4 slot 撑不住；
- 字幕场景**没有术语表可用**（谁去维护一场比赛的球员译名？）；
- 注入的「只输出译文」还会破坏 App 的 JSON 协议。

**结论**：字幕场景直接否掉。**桌面版仍保留这条链路**（有用），但手机 plain 路径不再走它。

---

### B. 整轨预热 + cue 缓存（`GATEWAY_SHIM=0` 且整轨开启，已下线）

```
WarmPing → /yt/warm → yt-dlp 拉整条 json3 → 去重 → 1400 字/批 × 3 并发翻译
        → SQLite(tracks + cue_index) → 播放时逐块请求命中 cue_index（57ms）
```

| 项 | 数据 |
|---|---|
| 整轨耗时 | 200 条 / 36s（fetch 3.5s + 翻译 32s） |
| 播放命中 | 0.01–0.02s |
| **实际命中率** | **9 / 47（19%）** |

**优点**：命中时极快；播放期间的模型调用几乎为零；拖动进度也不重复花钱。

**缺点（致命）**：
- App 的 block 是 **token 区间**，经常把一条字幕切成两半 → `cue_index` 按「条」索引天然对不上；
  实测 MISS 的块与整轨文本 `difflib` 覆盖率 **1.00**，纯粹是**边界不齐**；
- 返回「超集译文」违反「引文完整划分」→ App 判无效 → 重试；
- 需要 YouTube 抓轨：429（`en` 自动翻译轨）、`Sign in to confirm you're not a bot`；
- 预热期间要 hold，一旦 hold 判断写错就是长时间「翻译中」（历史事故见 §4）；
- 状态机复杂：`queued/fetching/working/done/error` + `ready_ms` + LRU + 去重 + window。

**结论**：命中率结构性偏低，性价比不成立。代码保留（`gateway/yt_subs.py`），默认关闭。

---

### C. 透明转发（`GATEWAY_PASSTHROUGH=1`，仅用于观测）

```
App 请求 → 网关原样转发上游 /chat/completions → 原样回传
         （只记录 ms / prompt / completion / reply 前 300 字）
```

| 项 | 数据 |
|---|---|
| 单块耗时 | **0.8s → 2s → 4s → 7.3s（递增）** |
| 结果 | 模型把输入 JSON **整段抄回来** → App 重试 → 卡「翻译中」 |

**优点**：最诚实——能观测「原版协议 + 本地模型」到底行不行；零适配成本。

**缺点**：本地 7B 无法满足该协议（下面复现数据），实际不可用。

复现（4 条字幕 / 309 字，同一 block）：

| 方式 | 耗时 | 结果 |
|---|---|---|
| 原版 system prompt | 768ms | JSON 合法，但**只翻了前 2 条**，未覆盖完整 `source_text` |
| 原版 + `json_schema` | 960ms | 同上 |
| 换成精简中文指令 | 2629ms | `source` 写成中文，彻底失败 |

**结论**：作为**观测与对照工具**保留，不用于生产。

---

### D. 协议适配层（`GATEWAY_SHIM=1`，★ 当前线上）

```
App 请求 {block, source_text, owned_tokens, context_*}
  ↓ 网关切句（正则，切分可逆：拼接结果 === source_text）
  ↓ 编号批量 → hy7b（600 字/批；缺行只重问缺的那几条；最多 3 轮，再不行逐条单发）
  ↓ 网关拼回 {block: 原 block, events:[{source: 网关切的句子, text: 译文}]}
  ↓ 完全相同 source_text 命中内存 LRU（0ms，512 条 / 30 分钟）
App 校验引文 → 通过
```

| 项 | 数据 |
|---|---|
| 单块耗时（41 次实播） | **avg 554ms / p50 561ms / p90 920ms / max 1447ms** |
| >2s 的请求 | **0** |
| 平均块规模 | 183 字符 / 3.2 条事件 |
| App 重试 | 基本消失（仅 `[music]` 这类真实重复） |
| 拖动进度 | 命中 LRU，端到端 62–65ms |

不同 block 规模（真实字幕轨，全部「引文完整划分 = True」）：

| block | 耗时 |
|---|---|
| 4 条 / 309 字 | 728ms |
| 10 条 / 722 字 | 1.41s |
| 20 条 / 1465 字 | 1.77s |
| 40 条 / ~2900 字 | 3.72s |

**优点**：
- 两种失败模式（引文不齐、外层 JSON 不对）都被**从模型手里拿走**，模型只剩「第 N 行 → 第 N 行」；
- 没有整轨抓取 → 不再需要 yt-dlp / 代理 / cookies，也躲掉 429 与 bot check；
- 无状态（除 LRU），拖动、跳转、切视频都不会污染；
- 代码量：`gateway/shim.py` 约 190 行。

**缺点**：
- 切句粒度由网关决定，可能和 App 期望的「展示行」不完全一致（App 自己会做折行）；
- 放弃 App 的 `context_before/after`、`avoid_event_end_after` 等高级提示 → 跨句依赖（代词指代）偶尔不如整块翻译；
- 需要接受「不做术语表」这件事。

---

## 2. 当前线上配置（唯一事实来源）

```bash
# /opt/immersive-translator-gateway/.env
GATEWAY_SHIM=1              # D：协议适配层（线上）
GATEWAY_PASSTHROUGH=0       # C：改 1 则变透明转发（仅观测）
# 整轨方案（B）相关开关全部不生效，/yt/warm 直接返回 disabled
```

行为：

```bash
curl 'http://192.168.2.97:8787/a2bxj5/yt/warm?v=xxx'
# {"status":"disabled","video_id":"xxx","reason":"shim_mode"}
```

桌面版沉浸式翻译走 `/v1/chat/completions`（无 `response_format`）→ **仍走 A 的注入/缓存链路**，
不受 shim 影响。

---

## 3. 关键坑（踩过的，别再踩）

| 坑 | 现象 | 正解 |
|---|---|---|
| `--extractor-args youtube:` **多次传参后者覆盖前者** | 静默变成 `no subtitles for the requested languages` | 合成一次：`youtube:player_client=X;player_skip=webpage,configs` |
| `--sub-langs en` 是**自动翻译轨** | `HTTP Error 429` | 先取 `en-orig`（原始轨）；两者合并请求 `"en-orig,en"` 更省一次往返 |
| 默认 client 撞 bot check | `Sign in to confirm you're not a bot` | 换 client（`android_vr` / `tv_embedded` / `android`），失效的 client 不要阻塞其他 client → 并行拉 |
| App 的 block 会切断字幕条 | 整轨缓存只命中 19% | 别按「条」索引；要按 block 现场切句（方案 D） |
| hold 判断写成全局的 | 别的视频一预热，当前视频全变「翻译中」 | hold 必须绑定「当前视频 + 时间位置」；方案 D 干脆不需要 hold |
| 模型抄输入 | 回复里带 `source_text` 字段 | 别让模型负责外层 JSON（方案 D） |

---

## 4. 历史事故：整轨方案下「翻译中」反复出现

日志证据（2026-10-31 夜）：

```
23:22:59  GET /yt/warm?v=NrKWR562rX8   (x2)
23:23:05  GET /yt/warm?v=LVH8BTJV2Go   (x4)
23:25-23:30  POST /v1/plain/chat/completions ... 200 OK  ← 全是空 events
ERROR: [youtube] LVH8BTJV2Go: Sign in to confirm you're not a bot.
```

三个 bug 叠加：hold 全局化 → 任一视频预热/失败都压制当前视频的命中；失败后 hold 600s；
多 cue block 匹配不上。**这就是后来整轨方案被否掉的直接原因。**

---

## 5. 开关与观测

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_SHIM` | `1` | 协议适配层（生产） |
| `GATEWAY_PASSTHROUGH` | `0` | 1 = 透明转发（观测） |
| `SHIM_BATCH_CHARS` | `600` | 每批字符上限 |
| `SHIM_MAX_CLAUSE` | `160` | 单句超长则按空白二次切（保持可逆） |
| `SHIM_MIN_CLAUSE` | `12` | 过短片段并入相邻句 |
| `SHIM_LRU_MAX` / `SHIM_LRU_TTL` | `512` / `1800` | 完全相同块的内存缓存 |
| `YT_PROXY` | — | 仅整轨方案用；shim 模式不读 |
| `YT_CLIENTS` | `android_vr,tv_embedded,,android` | 仅整轨方案用 |

日志（`/opt/immersive-translator-gateway/data/yt_debug.log`，一行一 JSON）：

```
SHIM / SHIM_FAIL          # 适配层成功/失败
PT                        # 透明转发
REQ_HIT / REQ_MISS / REQ_MODEL   # 整轨缓存命中/未命中/回落模型（B 方案）
WARM_* / FETCH_* / BATCH / WINDOW
```

端点：

```bash
GET /a2bxj5/yt/debug            # active + 各视频进度 + 最近 40 行日志
GET /a2bxj5/yt/debug/summary    # 事件计数 + 批次耗时统计
GET /a2bxj5/yt/debug/clear      # 清空日志（做一次干净实验用）
```

---

## 6. 回滚

```bash
# 退回整轨版（B）
ssh root@192.168.2.97
cd /opt && tar xzf immersive-translator-gateway.track-20261101-0003.tar.gz
sed -i 's/^GATEWAY_SHIM=.*/GATEWAY_SHIM=0/' /opt/immersive-translator-gateway/.env
sed -i 's/^GATEWAY_PASSTHROUGH=.*/GATEWAY_PASSTHROUGH=0/' /opt/immersive-translator-gateway/.env
systemctl restart immersive-translator
```

```bash
# 只切到透明转发（C，观测原版协议行为）
sed -i 's/^GATEWAY_PASSTHROUGH=.*/GATEWAY_PASSTHROUGH=1/' /opt/immersive-translator-gateway/.env
systemctl restart immersive-translator
```

---

## 7. 时间线

| 日期 | 变更 | 结论 |
|---|---|---|
| 10-31 早 | 整轨预热 + cue 缓存上线 | 首版「等一次、之后全命中」 |
| 10-31 晚 | 发现 429 真因（`en` 自动翻译轨）、加 SSH 隧道走路由器 PassWall | 抓轨可用 |
| 10-31 夜 | 实播日志：命中只有 9/47，`翻译中` 反复 | 整轨方案命中率结构性偏低 |
| 11-01 00:03 | 备份整轨版，加 `GATEWAY_PASSTHROUGH` | 观测原版协议 |
| 11-01 00:20 | 透明模式实测：模型抄输入、0.8→7.3s 递增 | 原版协议 + 本地 7B 不可用 |
| 11-01 00:40 | 协议适配层 `shim.py` 上线 | avg 554ms，repeats 0ms |
| 11-01 01:00 | 实播 12 分钟 + 多次拖动：流畅 | **定稿：方案 D** |

---

## 8. 桌面版与手机版：两套体系的关系

同一个网关进程里跑两条互斥的链路，**按 URL 路径分流**，不靠猜测：

| | 桌面浏览器（沉浸式翻译扩展） | 手机（Morphe AI 字幕） |
|---|---|---|
| 入口 | `POST /v1/chat/completions` | `POST /v1/plain/chat/completions` |
| 判定 | `is_plain_path = path.endswith("/v1/plain/...")` | 同左 |
| 处理 | 语境分析 + 术语注入 + 占位符保护 + 高频缓存 | 协议适配层 `shim.py`（切句 + 编号批量 + LRU） |
| 是否整轨 | 否 | 否（整轨已下线） |
| 输出 | 纯译文 | `{block, events:[{source,text}]}` |
| 状态 | 术语库/语境库（`terms.db`） | 无状态（仅内存 LRU） |

**不会冲突的三个理由：**

1. 分流是**路径级**的，不是内容猜测：`GATEWAY_SHIM` / `GATEWAY_PASSTHROUGH` 只对 `/v1/plain/` 生效；
   桌面请求即使带 `response_format` 也不会被 shim 接管（只跳过注入/缓存）。
2. 状态不共享：术语库只服务桌面；shim 的 LRU 只服务手机；整轨表（`subs.db`）在 shim 模式完全不读。
3. 唯一共享的是**上游模型**（hy7b，llama.cpp 4 slot）。

隔离实测（同一分钟连续三个请求）：

```
plain 路径              → {"block":"b0_x","events":[...]}     373ms
普通路径                → 纯译文（走桌面链路）                 92ms
普通路径 + response_format → 纯译文（仍走桌面链路）            260ms
日志：只有 plain 请求产生 SHIM 记录，无 REQ_* 噪声
```

并发实测（4 slot 模型）：

```
4 路字幕并发        各 520~580ms，整体 626ms
4 路字幕 + 1 路桌面  字幕 191/192ms（部分命中 LRU）+ 桌面 438ms，整体 442ms
```

> 也就是说：**同时用桌面翻译和手机字幕时，会共用 4 个模型 slot，极端并发下互相排队**，
> 但正常使用（一次播放 + 偶尔网页翻译）感知不到。

---

## 9. 稳定性与开机自启（2026-11-01 核查）

### 服务单元

```ini
# /etc/systemd/system/immersive-translator.service
[Unit]
Description=Immersive Translator Context & Term Gateway
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/immersive-translator-gateway
EnvironmentFile=/opt/immersive-translator-gateway/.env
ExecStart=/opt/immersive-translator-gateway/.venv/bin/python3 -m uvicorn gateway.server:app --host 0.0.0.0 --port 8787
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

| 核查项 | 结果 |
|---|---|
| 开机自启 | `enabled`，`/etc/systemd/system/multi-user.target.wants/immersive-translator.service` 符号链接存在 |
| 崩溃自愈 | `Restart=always` + `RestartSec=3` |
| 本次启动后错误 | **0**（`Traceback/ERROR/CRITICAL` 计数为 0） |
| 内存 | ~37 MB（稳定，无增长趋势） |
| 配置来源 | `EnvironmentFile=.env`（`GATEWAY_SHIM=1` 持久化在文件里，不依赖 shell 环境） |
| 路径 | 全部绝对路径；`.venv/bin/python3` 为符号链接到 `/usr/bin/python3` |
| 数据 | `/opt/immersive-translator-gateway/data/` 持久化（`terms.db`、`subs.db`、`yt_debug.log`） |
| 重启后状态 | 无内存态依赖：LRU 丢失只影响首次命中（几十 ms），不影响功能 |

### 附带的自启单元

```ini
# /etc/systemd/system/router-passwall-socks.service  （SSH 隧道 → 路由器 SOCKS）
After=network-online.target
Wants=network-online.target
Restart=always
```

`enabled`，开机自动拉起；**shim 模式下并不需要它**（只有整轨方案才用代理抓字幕），
保留是因为回滚整轨方案时可以直接用。它的免密依赖 `/root/.ssh/id_rsa`（已存在，实测 `ssh root@192.168.2.1` 免密可用）。

### 已知的两个小隐患（不影响使用）

1. `data/gateway_live.log` 会一直增长（当前 3.5MB，无轮转）—— 需要的话加 `logrotate`；
2. 上游模型 4 个 slot 由两条链路共享，见 §8 的并发实测。

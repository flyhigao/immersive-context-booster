# 沉浸式翻译 · 智能语境与术语伴侣 (Immersive Context Booster)

> 专为 **Reddit 论坛**（如 r/nba 篮球社区）与 **YouTube 视频/字幕翻译** 打造的 AI 语境分析与专业术语沉淀系统。

---

## 解决的核心痛点

1. **分块翻译缺乏上下文**：
   - 沉浸式翻译是按段落（甚至单句字幕）切片发给大模型的。
   - 面对 Reddit 俚语或 YouTube 视频字幕时，模型不知道整篇帖子的主题，将 *clutch* 误翻为“离合器”、*brick* 翻为“砖头”、*drop coverage* 翻为“掉落覆盖”、*tanking* 翻为“开坦克”。
2. **缺乏术语沉淀与管理**：
   - 专有名词和黑话无法自动积累，每次翻译都需要碰运气。

---

## 系统工作原理

```
[ Reddit / YouTube 网页 ]
          │
          ├── (1. 自动提取页面/视频标题、板块与背景摘要)
          ▼
[ 油猴脚本 (Tampermonkey) ] ───► [ 本地 AI 智能网关 (127.0.0.1:8787) ]
                                      ├─ 1. 深度分析主题与表达风格
                                      ├─ 2. 自动提取与沉淀专业术语库
                                      └─ 3. 动态将语境与术语注入 System Prompt
                                                ▲
[ 沉浸式翻译 (Firefox) ] ────────────────────────┘
  (BaseURL 指向 127.0.0.1:8787)
```

---

## 3 步使用指南

### 第 1 步：启动本地智能网关服务

在本项目目录下运行：
```bash
./run.sh
```
首次运行会自动创建 Python 虚拟环境并安装极简依赖（FastAPI、httpx、uvicorn）。
启动成功后，浏览器访问 **http://127.0.0.1:8787** 即可打开**术语与语境管理控制台**。

---

### 第 2 步：安装油猴脚本

1. 在 Firefox 中打开 **Tampermonkey（油猴）** 插件图标 -> 点击 **“添加新脚本”**。
2. 打开本项目下的脚本文件：`translateplugin/userscript/immersive-context-booster.user.js`。
3. 全选复制其中的内容，粘贴到油猴编辑器中，按 `Ctrl + S` 保存。
4. 现在打开任意 Reddit 帖子（如 r/nba）或 YouTube 视频，页面右下角会出现一个精致的 **🏀 语境伴侣胶囊**，自动同步当前页面的讨论主题与背景！

---

### 第 3 步：配置“沉浸式翻译”插件

打开沉浸式翻译的 **设置** -> **翻译服务** -> **自定义模型 (OpenAI)**：

- **自定义 API 地址 (API URL)**：`http://127.0.0.1:8787/v1/chat/completions`
- **模型名称 (Model)**：`hy7b`
- **API Key**：保留原来的即可（例如 `your_api_key` 或填 `custom`）

> **完成！** 之后你在 Reddit 看帖或看 YouTube 视频时，沉浸式翻译的每一次段落翻译和字幕翻译，都会自动携带当前页面/视频的全局语境和专业术语库，再也不会出现离合器、砖头等灾难性直译！

---

## 可选：手机 / YouTube App 内 AI 字幕

手机端客户端（如 Morphe 补丁 `AI caption translator`、PolyglotYT 等）会在自己的 System Prompt 里写死 **JSON Schema 输出协议**，任何额外注入都会破坏其解析。所以网关提供了一个**无注入直通入口**：

- `POST http://127.0.0.1:8787/v1/plain/chat/completions`
- 或者任意请求体里带 `response_format` 时自动进入该模式

该模式下：不注入语境 / 术语 / 人名规则，不读写翻译缓存，只做模型名改写与转发。

### 手机连 USB 时打通网关

```bash
# 电脑照常 ./run.sh（网关绑定 127.0.0.1，不需要改）
adb devices
adb reverse tcp:8787 tcp:8787   # 手机的 127.0.0.1:8787 → 电脑的 127.0.0.1:8787
adb reverse --list              # 重新插拔 USB 后需要重跑
```

### YouTube App 内接法（Morphe + AI caption translator）

1. 安装 [Morphe Manager](https://github.com/MorpheApp/morphe-manager/releases/latest)，设置里开启「专家模式」；
2. 补丁源 → 添加远程：`https://github.com/YYDarlinker/morphe-ai-caption-translator`；
3. 用**原版 YouTube APK**（项目声明目标：21.30.216 / 21.42.241 / 21.43.523，后两个为实验性）打补丁，保留官方默认补丁组合；
4. YouTube → 设置 → Morphe → 视频 → AI字幕翻译：
   - API 地址：`http://127.0.0.1:8787/v1/plain/chat/completions`
   - 密钥：随意填（网关不校验入站 key）
   - 模型：`hy7b`（网关会用 `DEFAULT_MODEL` 覆盖该字段）
5. 播放视频 → CC → 字幕菜单选「自动翻译 → 中文」。

> 说明：YouTube 自带 `network_security_config` 的 `base-config` 已是 `cleartextTrafficPermitted=true`（仅对 youtube.com / googleapis.com 关闭），因此 `http://127.0.0.1:8787` 可直接访问，无需额外改包。

---

## 功能特性

1. **预置 80+ 条 NBA/篮球专业术语与论坛黑话**：
   - *clutch* -> 关键时刻/大心脏
   - *brick* -> 打铁/投丢
   - *posterize* -> 隔扣/扣成背景板
   - *drop coverage* -> 沉退防守
   - *PnR / pick and roll* -> 挡拆
   - *tanking* -> 摆烂
   - *garbage time* -> 垃圾时间
   - *ring chaser* -> 抱团争冠老将
   - *Chef Curry / King James / Wemby / Slim Reaper* 等球员外号
2. **当前页面 AI 预分析**：
   - 进入页面/视频后，后台调用 `hy7b` 自动推断领域、核心讨论主题、表达风格与背景简述。
3. **术语自动提取与沉淀**：
   - 在分析页面以及翻译过程中，AI 会自动识别生僻的专业术语并提取沉淀到本地 SQLite 数据库。
4. **可视化 Web 管理面板 (http://127.0.0.1:8787)**：
   - 查看当前生效的网页语境；
   - 搜索、添加、编辑、删除术语；
   - 一键导出术语库 JSON。

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/subtitle-pipeline.md`](docs/subtitle-pipeline.md) | **Android 字幕链路四种实现的实测数据、优劣对比、当前运行哪一种、如何切换与回滚** |
| `immersive-context-booster`（本仓库） | 网关本体：注入/术语/缓存（桌面版）+ 协议适配层（手机字幕） |
| [androidyoutube_translate](https://github.com/flyhigao/androidyoutube_translate) | 部署手册、客户端配置路径与 Cookie 生命周期、踩坑记录 |

## Android 字幕：协议适配层（当前推荐，2026-11）

`gateway/shim.py` 用于手机端 AI 字幕（Morphe RebuildProtocol）：

- App 要求回复里的 `source` 引文**逐字完整划分** `source_text`，7B 模型做不到 →
  频繁重试 → 一直「翻译中」
- shim 由**网关自己切句**，模型只做「第 N 行 → 译文」，网关再拼回 App 的 JSON
- 实测：41 次请求 avg 554ms / p90 920ms / max 1.45s，无重试
- 完全不走术语注入、语境注入、整轨缓存

| 开关 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_SHIM` | `1` | 协议适配层 |
| `GATEWAY_PASSTHROUGH` | `0` | 1 = 透明转发，只记日志 |
| `SHIM_BATCH_CHARS` | `600` | 每批字符上限 |
| `SHIM_LRU_MAX` / `SHIM_LRU_TTL` | `512` / `1800` | 完全相同块的内存缓存 |

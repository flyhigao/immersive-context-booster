"""
整轨字幕预翻译（B 路线服务端）

流程：yt-dlp 拉整条字幕轨 → 去重 → 分批（并发）翻译 → SQLite 缓存 → 返回 json3。
另提供 cue 级模糊命中（A 方案），供逐块请求直接返回已翻好的整轨译文。
"""
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from gateway.config import settings

SUBS_DB = os.path.join(os.path.dirname(settings.DATABASE_PATH), "subs.db")
YTDLP_DIR = os.getenv("YTDLP_DIR", "/tmp/yt-subs")
BATCH_CHARS = int(os.getenv("YT_BATCH_CHARS", "1400"))
BATCH_CONCURRENCY = int(os.getenv("YT_CONCURRENCY", "3"))
TARGET_LANG = os.getenv("YT_TARGET_LANG", "zh-Hans")
# 按时间窗口推进：先把前 N 秒的字幕翻好，播放器就能立刻出字幕，剩余部分后台继续。
WINDOW_SECONDS = float(os.getenv("YT_WINDOW_SECONDS", "120"))
# 第一窗口更小，只要几秒就能出第一批字幕
FIRST_WINDOW_SECONDS = float(os.getenv("YT_FIRST_WINDOW_SECONDS", "30"))
LOG_PATH = os.getenv("YT_DEBUG_LOG", "/opt/immersive-translator-gateway/data/yt_debug.log")

# 当前正在观看的视频（由 WarmPing 告知），用于把 hold 限定在这一个视频上。
ACTIVE: Dict[str, Any] = {"video_id": None, "t": 0.0}


def log_event(kind: str, **kw: Any) -> None:
    """结构化调试日志：一行一个 JSON，方便 grep/统计。"""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        rec = {"ts": round(time.time(), 3), "event": kind}
        rec.update(kw)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def set_active(video_id: str) -> None:
    ACTIVE["video_id"] = video_id
    ACTIVE["t"] = time.time()
    log_event("ACTIVE", vid=video_id)

NUMBERED_RE = re.compile(r"^(\d+)\s*[\t.:、]\s*(.+)$")

# 每个视频的预热进度（内存态，重启即清空）
PROGRESS: Dict[str, Dict[str, Any]] = {}


def video_id_from(text: str) -> Optional[str]:
    """从 URL / 纯 ID 里取出 11 位视频 ID。"""
    text = (text or "").strip()
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    return None


def any_warming(max_age_s: int = 600) -> bool:
    """是否有正在进行的预热（用于逐块请求的“先别翻，等整轨”策略）。"""
    now = time.time()
    for p in PROGRESS.values():
        if p.get("state") in ("queued", "fetching", "working") and now - p.get("t0", 0) < max_age_s:
            return True
    return False


def any_failed(max_age_s: int = 600) -> bool:
    """最近是否有预热失败。失败后也不能退回逐句翻译，否则播放器会卡在“翻译中”。"""
    now = time.time()
    for p in PROGRESS.values():
        if p.get("state") == "error" and now - p.get("t0", 0) < max_age_s:
            return True
    return False


def should_hold(text: str = "") -> bool:
    """逐块请求是否应该返回空 events。
    只对「当前正在观看、且这一段还没轮到翻译」的字幕 hold，不会被其他视频的预热连带影响。"""
    vid = ACTIVE.get("video_id")
    p = PROGRESS.get(vid) if vid else None
    if not p:
        return False
    if p.get("state") == "done":
        return False
    if p.get("state") in ("queued", "fetching"):
        # 连轨都还没拿到，不知道这段在哪：只短暂 hold（默认 10s），避免长期无字幕
        return time.time() - p.get("t0", 0) < float(os.getenv("YT_FETCH_HOLD_SECONDS", "10"))
    if p.get("state") == "working":
        if not text:
            return True
        pos = cue_position(vid, text)
        if pos is None:
            return False          # 不在当前轨里，别拦，交给模型翻
        return pos > int(p.get("ready_ms") or 0)   # 还没翻到这一段
    if p.get("state") == "error":
        return os.getenv("YT_HOLD_ON_ERROR", "true").lower() in ("true", "1", "yes") \
            and time.time() - p.get("t0", 0) < float(os.getenv("YT_HOLD_ON_ERROR_SECONDS", "60"))
    return False


def progress(video_id: str) -> Dict[str, Any]:
    p = PROGRESS.get(video_id)
    if p and p.get("state") in ("queued", "fetching", "working", "done", "error"):
        total = max(1, p.get("total", 0))
        return {
            "state": p.get("state", "working"),
            "total": p.get("total", 0),
            "done": p.get("done", 0),
            "percent": round(100.0 * p.get("done", 0) / total, 1),
            "ready_s": round((p.get("ready_ms") or 0) / 1000.0, 1),
            "elapsed": round(time.time() - p.get("t0", time.time()), 1),
        }
    if cache_get(video_id, "en", TARGET_LANG):
        return {"state": "done", "total": 0, "done": 0, "percent": 100.0, "cached": True}
    return {"state": "unknown", "total": 0, "done": 0, "percent": 0.0}


def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(SUBS_DB), exist_ok=True)
    conn = sqlite3.connect(SUBS_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tracks("
        " video_id TEXT, src TEXT, tgt TEXT, payload TEXT, created_at INTEGER,"
        " PRIMARY KEY(video_id, src, tgt))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cue_index("
        " video_id TEXT, idx INTEGER, norm TEXT, zh TEXT, ts INTEGER, ms INTEGER DEFAULT 0,"
        " src TEXT DEFAULT 'en', tgt TEXT DEFAULT 'zh-Hans',"
        " PRIMARY KEY(video_id, idx))"
    )
    # 旧库补列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cue_index)")}
    for name, decl in (("ms", "INTEGER DEFAULT 0"), ("src", "TEXT DEFAULT 'en'"),
                       ("tgt", "TEXT DEFAULT 'zh-Hans'")):
        if name not in cols:
            conn.execute(f"ALTER TABLE cue_index ADD COLUMN {name} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cue_norm ON cue_index(norm)")
    conn.commit()
    return conn


def cache_get(video_id: str, src: str, tgt: str) -> Optional[Dict[str, Any]]:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT payload FROM tracks WHERE video_id=? AND src=? AND tgt=?",
            (video_id, src, tgt),
        ).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def cache_put(video_id: str, src: str, tgt: str, payload: Dict[str, Any]) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tracks VALUES(?,?,?,?,?)",
            (video_id, src, tgt, json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


_LAST_FETCH = [0.0]
MIN_FETCH_INTERVAL = float(os.getenv("YT_MIN_FETCH_INTERVAL", "3"))
COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "/opt/immersive-translator-gateway/data/yt-cookies.txt")
# 2.97 直连出口会被 YouTube 限流（429），默认走路由器 PassWall 的 SOCKS 隧道。
YT_PROXY = os.getenv("YT_PROXY", "")


def _lang_candidates(src_lang: str) -> List[str]:
    """YouTube 对自动翻译轨（en）常返 429，但原始轨（en-orig）能下载。
    第一轮把两者合并请求：en-orig 成功就拿，en 的 429 只是噪声。"""
    base = (src_lang or "en").split("-")[0]
    out = [f"{base}-orig,{base}", f"{base}-orig", base, f"{base}.*"]
    if base != "en":
        out += ["en-orig,en", "en"]
    return out


# 默认 client 有些视频会撞 “Sign in to confirm you're not a bot”；换 client 就能拿到字幕。
# 空字符串 = yt-dlp 默认多 client 组合。
_CLIENT_CANDIDATES = os.getenv("YT_CLIENTS", "android_vr,tv_embedded,,android").split(",")


def _cleanup(video_id: str) -> None:
    for f in os.listdir(YTDLP_DIR):
        if f.startswith(video_id):
            try:
                os.remove(os.path.join(YTDLP_DIR, f))
            except OSError:
                pass


def _read_downloaded(video_id: str) -> Optional[Dict[str, Any]]:
    for f in sorted(os.listdir(YTDLP_DIR)):
        if f.startswith(video_id) and f.endswith(".json3"):
            path = os.path.join(YTDLP_DIR, f)
            if os.path.getsize(path) > 64:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
    return None


def _build_cmd(video_id: str, langs: str, client: str = "", tag: str = "") -> List[str]:
    # 注意：多次 --extractor-args youtube:... 是“后者覆盖前者”，必须合成一个；
    # player_skip 与 player_client 分开发会被吃掉，导致 “no subtitles”。
    yt_args = "player_skip=webpage,configs"
    if client:
        yt_args = f"player_client={client};{yt_args}"
    stem = f"{video_id}.{tag}" if tag else video_id
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings",
        "--skip-download",
        # 只要字幕：跳过网页/播放器配置，抓轨从 4.6s 降到 2.4s
        "--extractor-args", f"youtube:{yt_args}",
        # 没有可用视频格式也不影响字幕
        "--ignore-no-formats-error",
        "--write-subs", "--write-auto-subs",
        "--sub-langs", langs,
        "--sub-format", "json3",
        "-o", os.path.join(YTDLP_DIR, f"{stem}.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    if os.path.exists(COOKIES_FILE):
        cmd[3:3] = ["--cookies", COOKIES_FILE]
    if YT_PROXY:
        cmd[3:3] = ["--proxy", YT_PROXY]
    return cmd


def _rows_for_block(norm: str, want_zh: bool = True) -> List[Tuple[str, Optional[str], str, int, int]]:
    """取出长度相近的候选条（优先当前视频）。返回 (norm, zh, video_id, idx, ms)。"""
    conn = _db()
    try:
        lo, hi = max(8, int(len(norm) * 0.2)), int(len(norm) * 2.0) + 20
        sql = (
            "SELECT norm, zh, video_id, idx, ms FROM cue_index"
            " WHERE length(norm) BETWEEN ? AND ?"
        )
        if want_zh:
            sql += " AND zh IS NOT NULL"
        sql += " ORDER BY ts DESC LIMIT 2000"
        return conn.execute(sql, (lo, hi)).fetchall()
    finally:
        conn.close()


def _rows_active(active: str) -> List[Tuple[str, Optional[str], int, int]]:
    conn = _db()
    try:
        return conn.execute(
            "SELECT norm, zh, idx, ms FROM cue_index WHERE video_id=? ORDER BY ms", (active,)
        ).fetchall()
    finally:
        conn.close()


def compose_lookup(text: str) -> Optional[Dict[str, Any]]:
    """App 的 block 常常跨 2~3 条字幕，拼起来的 norm 不等于任何单条。
    这里把 block 文本按时间顺序用已翻译的 cue 拼回去：全部拼上就算命中。"""
    norm = normalize(text)
    if len(norm) < 12:
        return None
    active = ACTIVE.get("video_id")

    for vid in ([active] if active else []) + [None]:
        rows = _rows_active(vid) if vid else [
            (r[0], r[1], r[2], r[4]) for r in _rows_for_block(norm, want_zh=False)
        ]
        if not rows:
            continue
        by_norm = {r[0]: r for r in rows}
        pos, parts, matched, first_ms, covered = 0, [], [], None, 0
        while pos < len(norm):
            best = None
            for take in (220, 160, 120, 90, 60, 40, 24, 16, 12):
                chunk = norm[pos:pos + take].strip()
                if chunk and chunk in by_norm:
                    best = by_norm[chunk]
                    pos += take
                    break
            if best is None:
                nxt = norm.find(" ", pos + 1)
                if nxt < 0:
                    break
                pos = nxt + 1
                continue
            matched.append(best)
            covered += len(best[0])
            if first_ms is None:
                first_ms = int(best[3] or 0)
            if best[1]:
                parts.append(best[1])
        if not matched:
            continue
        if covered >= 0.9 * len(norm) and parts:
            return {"score": 1.0, "text": "".join(parts), "video_id": vid,
                    "idx": matched[0][2], "ms": first_ms}
    return None


def _read_tagged(video_id: str, tag: str) -> Optional[Dict[str, Any]]:
    """并行抓取时每个 client 写到不同文件名，取任意一个已有内容的。"""
    prefix = f"{video_id}.{tag}"
    for f in sorted(os.listdir(YTDLP_DIR)):
        if f.startswith(prefix) and f.endswith(".json3"):
            path = os.path.join(YTDLP_DIR, f)
            if os.path.getsize(path) > 64:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
    return None


def _fetch_parallel(video_id: str, langs: str, clients: List[str],
                    overall_timeout: float = 45.0) -> Tuple[Optional[Dict[str, Any]], str]:
    """多个 client 同时拉：谁先拿到字幕用谁。比串行省 3~10s（撞 bot check 的 client 不阻其他）。"""
    procs: List[Tuple[subprocess.Popen, str, str]] = []
    for i, client in enumerate(clients):
        tag = f"c{i}"
        cmd = _build_cmd(video_id, langs, client, tag=tag)
        try:
            procs.append((subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL), tag, client))
        except Exception:
            pass

    deadline = time.time() + overall_timeout
    while time.time() < deadline:
        for _proc, tag, client in procs:
            track = _read_tagged(video_id, tag)
            if track is not None:
                for p, _, _ in procs:
                    if p.poll() is None:
                        p.terminate()
                log_event("FETCH_OK", vid=video_id, langs=langs, client=client or "default",
                          events=len(track.get("events") or []))
                return track, client
        if all(p.poll() is not None for p, _, _ in procs):
            break
        time.sleep(0.25)

    for p, _, _ in procs:
        if p.poll() is None:
            p.terminate()
    for p, _, _ in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
    return None, ""


def fetch_track(video_id: str, src_lang: str = "en") -> Optional[Dict[str, Any]]:
    """拉整条 json3 字幕轨：多 client 并行 + 多语言候选。
    多数视频 2~3s 拿到；撞 429 或 bot check 的 client 不会拖慢其他 client。"""
    wait = MIN_FETCH_INTERVAL - (time.time() - _LAST_FETCH[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_FETCH[0] = time.time()

    os.makedirs(YTDLP_DIR, exist_ok=True)
    _cleanup(video_id)

    for langs in _lang_candidates(src_lang):
        track, client = _fetch_parallel(video_id, langs, _CLIENT_CANDIDATES)
        if track is not None:
            return track
        log_event("FETCH_LANGS_FAIL", vid=video_id, langs=langs)
    _cleanup(video_id)
    return None


def _cues(track: Dict[str, Any]) -> List[Tuple[int, str, int]]:
    """按事件下标取非空文本 + 起始毫秒，保证译文能原样写回同一个事件。"""
    out = []
    for i, ev in enumerate(track.get("events", [])):
        segs = ev.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs).strip()
        if text:
            out.append((i, text, int(ev.get("tStartMs") or 0)))
    return out


def _unique(
    cues: List[Tuple[int, str, int]],
) -> Tuple[List[Tuple[str, str, int]], Dict[str, List[int]]]:
    """自动字幕里同一句会重复出现（滚动字幕），去重后按时间顺序翻译，省一半 token。"""
    first: Dict[str, Tuple[str, int]] = {}
    mapping: Dict[str, List[int]] = {}
    for idx, text, ms in cues:
        key = normalize(text)
        if not key:
            continue
        mapping.setdefault(key, []).append(idx)
        if key not in first or ms < first[key][1]:
            first[key] = (text, ms)
    items = sorted(((k, v[0], v[1]) for k, v in first.items()), key=lambda x: x[2])
    return items, mapping


def _windows(
    items: List[Tuple[str, str, int]], span_ms: int, first_span_ms: int = 0
) -> List[List[Tuple[str, str, int]]]:
    """把待翻句子按时间切窗口，保证先出开头那段字幕。
    first_span_ms > 0 时，第一窗口单独变小，让第一批字幕尽快出。"""
    if span_ms <= 0:
        return [items] if items else []
    if first_span_ms and first_span_ms < span_ms:
        head = [it for it in items if it[2] <= first_span_ms]
        rest = [it for it in items if it[2] > first_span_ms]
        return ([head] if head else []) + _windows(rest, span_ms)
    out: List[List[Tuple[str, str, int]]] = []
    cur: List[Tuple[str, str, int]] = []
    edge = span_ms
    for it in items:
        if it[2] > edge and cur:
            out.append(cur)
            cur = []
            edge = it[2] + span_ms
        cur.append(it)
    if cur:
        out.append(cur)
    return out


def _batches(items: List[Tuple[str, str, int]], limit: int) -> List[List[Tuple[str, str, int]]]:
    batches: List[List[Tuple[str, str, int]]] = []
    cur: List[Tuple[str, str, int]] = []
    size = 0
    for key, text, ms in items:
        if cur and size + len(text) > limit:
            batches.append(cur)
            cur, size = [], 0
        cur.append((key, text, ms))
        size += len(text)
    if cur:
        batches.append(cur)
    return batches


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\[[^\]]*\]", " ", text)          # 去掉 [APPLAUSE] 这类标注
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_numbered(raw: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = NUMBERED_RE.match(line)
        if m:
            out[int(m.group(1)) - 1] = m.group(2).strip()
    return out


async def _post(client: httpx.AsyncClient, payload: Dict[str, Any], timeout: float) -> str:
    resp = await client.post(
        f"{settings.UPSTREAM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {settings.UPSTREAM_API_KEY}"},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


BATCH_SYSTEM = (
    "You translate numbered subtitle lines into {tgt}. Output exactly the same number of lines, "
    "in the same order, each starting with the same number and a tab character. "
    "Do not merge, split, add or drop lines. No explanations."
)


async def translate_texts(client: httpx.AsyncClient, texts: List[str], tgt: str) -> List[str]:
    """按“编号 + 制表符”批量翻译：实测比 JSON 数组更能保证条数对齐。"""
    if not texts:
        return []

    payload = {
        "model": settings.DEFAULT_MODEL,
        "temperature": 0.2,
        "stream": False,
        "max_tokens": max(256, sum(len(t) for t in texts) * 3),
        "messages": [
            {"role": "system", "content": BATCH_SYSTEM.format(tgt=tgt)},
            {"role": "user", "content": "\n".join(
                "{}\t{}".format(i + 1, t) for i, t in enumerate(texts))},
        ],
    }

    got = parse_numbered(await _post(client, payload, 180.0))
    result: List[str] = []
    for i, src in enumerate(texts):
        tv = got.get(i)
        if tv:
            result.append(tv)
            continue
        single = {
            "model": settings.DEFAULT_MODEL,
            "temperature": 0.2,
            "stream": False,
            "max_tokens": 256,
            "messages": [
                {"role": "system", "content": f"Translate into {tgt}. Output only the translation."},
                {"role": "user", "content": src},
            ],
        }
        result.append((await _post(client, single, 120.0)).strip())
    return result


async def translate_track(video_id: str, src: str = "en", tgt: str = TARGET_LANG) -> Dict[str, Any]:
    cached = cache_get(video_id, src, tgt)
    if cached:
        cached["_cache"] = "hit"
        n = int(cached.get("unique") or cached.get("cues") or 0)
        # 缓存命中也要落 done，否则 /yt/status 会永远停在 queued，播放器一直显示“翻译中”。
        PROGRESS[video_id] = {"state": "done", "total": n, "done": n, "t0": time.time()}
        log_event("WARM_CACHE_HIT", vid=video_id, cues=n)
        return cached

    t_start = time.time()
    PROGRESS[video_id] = {"state": "fetching", "total": 0, "done": 0, "t0": t_start}
    log_event("WARM_FETCH", vid=video_id, proxy=bool(YT_PROXY))
    track = await asyncio.to_thread(fetch_track, video_id, src)
    if not track:
        PROGRESS[video_id] = {"state": "error", "total": 0, "done": 0, "t0": time.time()}
        log_event("WARM_ERROR", vid=video_id, reason="no_caption_track",
                  secs=round(time.time() - t_start, 2))
        return {"error": "no_caption_track", "video_id": video_id}

    cues = _cues(track)
    unique, mapping = _unique(cues)
    log_event("WARM_TRACK", vid=video_id, cues=len(cues), unique=len(unique),
              secs=round(time.time() - t_start, 2))

    # 先建立「原文 -> 位置」索引（译文暂空），播放中就能判断“这一段轮到没”
    index_track(video_id, cues, {}, src=src, tgt=tgt)

    st: Dict[str, Any] = {"state": "working", "total": len(unique), "done": 0,
                          "t0": time.time(), "ready_ms": 0}
    PROGRESS[video_id] = st

    sem = asyncio.Semaphore(max(1, BATCH_CONCURRENCY))
    by_key: Dict[str, str] = {}
    translated: Dict[int, str] = {}

    async with httpx.AsyncClient() as client:
        async def run(batch: List[Tuple[str, str, int]]) -> None:
            async with sem:
                t0 = time.time()
                got = await translate_texts(client, [t for _, t, _ in batch], tgt)
                for (key, _, _), out in zip(batch, got):
                    by_key[key] = out
                st["done"] = st.get("done", 0) + len(batch)
                log_event("BATCH", vid=video_id, n=len(batch), secs=round(time.time() - t0, 2),
                          done=st["done"], total=st["total"])

        # 按时间窗口推进：第一个窗口翻完就立即可播，剩余后台继续
        for w_i, window in enumerate(
            _windows(unique, int(WINDOW_SECONDS * 1000), int(FIRST_WINDOW_SECONDS * 1000))
        ):
            await asyncio.gather(*[run(b) for b in _batches(window, BATCH_CHARS)])
            for key, idxs in mapping.items():
                zh = by_key.get(key)
                if zh:
                    for i in idxs:
                        translated[i] = zh
            st["ready_ms"] = max(it[2] for it in window) + 1
            log_event("WINDOW", vid=video_id, window=w_i, cues=len(window),
                      ready_ms=st["ready_ms"], ready_s=round(st["ready_ms"] / 1000.0, 1),
                      done=st["done"], total=st["total"],
                      secs=round(time.time() - t_start, 2))
            index_track(video_id, cues, translated, src=src, tgt=tgt)

    events = track.get("events", [])
    for idx, zh in translated.items():
        if 0 <= idx < len(events):
            events[idx]["segs"] = [{"utf8": zh}]

    result = {
        "video_id": video_id,
        "src": src,
        "tgt": tgt,
        "cues": len(cues),
        "unique": len(unique),
        "batches": len(_batches(unique, BATCH_CHARS)),
        "track": track,
        "_cache": "miss",
    }
    index_track(video_id, cues, translated, src=src, tgt=tgt)
    cache_put(video_id, src, tgt, result)
    PROGRESS[video_id] = {"state": "done", "total": len(unique), "done": len(unique),
                          "t0": st["t0"], "ready_ms": st["ready_ms"]}
    log_event("WARM_DONE", vid=video_id, cues=len(cues), unique=len(unique),
              translated=len(translated), secs=round(time.time() - t_start, 2))
    return result


# ----------------- 逐块请求的模糊命中（A 方案：不动 App） -----------------

def index_track(
    video_id: str,
    cues: List[Tuple[int, str, int]],
    translated: Dict[int, str],
    src: str = "en",
    tgt: str = TARGET_LANG,
) -> None:
    """全量入库：未翻好的条也写入（zh 为空），用于判断“这一段轮到没”。"""
    conn = _db()
    try:
        now = int(time.time())
        rows = []
        for idx, src_text, ms in cues:
            n = normalize(src_text)
            if len(n) < 8:
                continue
            rows.append((video_id, idx, n, translated.get(idx), now, ms, src, tgt))
        conn.executemany(
            "INSERT INTO cue_index(video_id,idx,norm,zh,ts,ms,src,tgt)"
            " VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(video_id,idx) DO UPDATE SET"
            " norm=excluded.norm, zh=COALESCE(excluded.zh, cue_index.zh),"
            " ts=excluded.ts, ms=excluded.ms",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def cue_position(video_id: str, text: str) -> Optional[int]:
    """这块文本在当前轨里的起始毫秒（用于判断是否已轮到翻译）。"""
    norm = normalize(text)
    if len(norm) < 12:
        return None
    conn = _db()
    try:
        lo, hi = int(len(norm) * 0.5) - 10, int(len(norm) * 2.0) + 20
        rows = conn.execute(
            "SELECT norm, ms FROM cue_index WHERE video_id=? AND length(norm) BETWEEN ? AND ?",
            (video_id, lo, hi),
        ).fetchall()
    finally:
        conn.close()
    padded = " " + norm + " "
    for cn, ms in rows:
        padded_cn = " " + cn + " "
        if padded in padded_cn or padded_cn in padded:
            return int(ms or 0)
    return None


def cue_lookup(text: str, min_score: float = 0.6) -> Optional[Dict[str, Any]]:
    """在已预翻译的整轨里找与这块文本对应的字幕条（包含匹配 + 长度比例打分）。
    当前正在观看的视频优先，避免不同视频的相似句子互相干扰。"""
    norm = normalize(text)
    if len(norm) < 12:
        return None

    active = ACTIVE.get("video_id")
    conn = _db()
    try:
        lo, hi = int(len(norm) * 0.5) - 10, int(len(norm) * 2.0) + 20
        rows = conn.execute(
            "SELECT norm, zh, video_id, idx FROM cue_index"
            " WHERE zh IS NOT NULL AND length(norm) BETWEEN ? AND ?"
            " ORDER BY ts DESC LIMIT 800",
            (lo, hi),
        ).fetchall()
    finally:
        conn.close()

    padded = " " + norm + " "
    best = None
    for cn, zh, video_id, idx in rows:
        padded_cn = " " + cn + " "
        if padded in padded_cn or padded_cn in padded:
            score = min(len(norm), len(cn)) / max(len(norm), len(cn))
            if score < min_score:
                continue
            if video_id == active:
                score += 0.5          # 同一视频优先
            if best is None or score > best["score"]:
                best = {"score": round(score, 3), "text": zh, "video_id": video_id, "idx": idx}
    return best


def resolve_by_title(title: str) -> Optional[Dict[str, str]]:
    """按媒体会话标题在 YouTube 搜索视频（yt-dlp ytsearch1），返回 {id,title}。"""
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--skip-download",
           "--print", "%(id)s\t%(title)s", f"ytsearch1:{title}"]
    if os.path.exists(COOKIES_FILE):
        cmd[3:3] = ["--cookies", COOKIES_FILE]
    if YT_PROXY:
        cmd[3:3] = ["--proxy", YT_PROXY]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return None
    vid, _, found = line[0].partition("\t")
    return {"id": vid.strip(), "title": found.strip()}

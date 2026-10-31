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


def progress(video_id: str) -> Dict[str, Any]:
    p = PROGRESS.get(video_id)
    if p and p.get("state") in ("queued", "fetching", "working", "done", "error"):
        total = max(1, p.get("total", 0))
        return {
            "state": p.get("state", "working"),
            "total": p.get("total", 0),
            "done": p.get("done", 0),
            "percent": round(100.0 * p.get("done", 0) / total, 1),
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
        " video_id TEXT, idx INTEGER, norm TEXT, zh TEXT, ts INTEGER,"
        " PRIMARY KEY(video_id, idx))"
    )
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
MIN_FETCH_INTERVAL = float(os.getenv("YT_MIN_FETCH_INTERVAL", "5"))
COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "/opt/immersive-translator-gateway/data/yt-cookies.txt")


def fetch_track(video_id: str, src_lang: str = "en") -> Optional[Dict[str, Any]]:
    """用 yt-dlp 抓整条 json3 字幕轨（人工轨优先，没有则自动字幕）。
    429 是 IP 级限流：串行 + 退避重试，避免越抓越慢。"""
    import random

    wait = MIN_FETCH_INTERVAL - (time.time() - _LAST_FETCH[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_FETCH[0] = time.time()

    os.makedirs(YTDLP_DIR, exist_ok=True)
    for f in os.listdir(YTDLP_DIR):
        if f.startswith(video_id):
            try:
                os.remove(os.path.join(YTDLP_DIR, f))
            except OSError:
                pass

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--skip-download",
        "--write-subs", "--write-auto-subs",
        "--sub-langs", src_lang,
        "--sub-format", "json3",
        "-o", os.path.join(YTDLP_DIR, "%(id)s.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    if os.path.exists(COOKIES_FILE):
        cmd[3:3] = ["--cookies", COOKIES_FILE]

    for attempt, backoff in enumerate((0, 3, 12, 30)):
        if backoff:
            time.sleep(backoff + random.uniform(0, 2))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        err = (proc.stderr or "") + (proc.stdout or "")
        for f in os.listdir(YTDLP_DIR):
            if f.startswith(video_id) and f.endswith(".json3"):
                path = os.path.join(YTDLP_DIR, f)
                if os.path.getsize(path) > 64:
                    with open(path, encoding="utf-8") as fh:
                        return json.load(fh)
        if "429" not in err:
            break
        _LAST_FETCH[0] = time.time()
    return None


def _cues(track: Dict[str, Any]) -> List[Tuple[int, str]]:
    """按事件下标取非空文本，保证译文能原样写回同一个事件。"""
    out = []
    for i, ev in enumerate(track.get("events", [])):
        segs = ev.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs).strip()
        if text:
            out.append((i, text))
    return out


def _unique(cues: List[Tuple[int, str]]) -> Tuple[List[Tuple[str, str]], Dict[str, List[int]]]:
    """自动字幕里同一句会重复出现（滚动字幕），去重后再翻译，通常能省一半 token。"""
    texts: Dict[str, str] = {}
    mapping: Dict[str, List[int]] = {}
    for idx, text in cues:
        key = normalize(text)
        if not key:
            continue
        if key not in texts:
            texts[key] = text
            mapping[key] = []
        mapping[key].append(idx)
    return list(texts.items()), mapping


def _batches(items: List[Tuple[str, str]], limit: int) -> List[List[Tuple[str, str]]]:
    batches: List[List[Tuple[str, str]]] = []
    cur: List[Tuple[str, str]] = []
    size = 0
    for key, text in items:
        if cur and size + len(text) > limit:
            batches.append(cur)
            cur, size = [], 0
        cur.append((key, text))
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
        return cached

    PROGRESS[video_id] = {"state": "fetching", "total": 0, "done": 0, "t0": time.time()}
    track = await asyncio.to_thread(fetch_track, video_id, src)
    if not track:
        PROGRESS[video_id] = {"state": "error", "total": 0, "done": 0, "t0": time.time()}
        return {"error": "no_caption_track", "video_id": video_id}

    cues = _cues(track)
    unique, mapping = _unique(cues)
    batches = _batches(unique, BATCH_CHARS)
    PROGRESS[video_id] = {"state": "working", "total": len(unique), "done": 0, "t0": time.time()}

    sem = asyncio.Semaphore(max(1, BATCH_CONCURRENCY))
    by_key: Dict[str, str] = {}

    async with httpx.AsyncClient() as client:
        async def run(batch: List[Tuple[str, str]]) -> None:
            async with sem:
                got = await translate_texts(client, [t for _, t in batch], tgt)
                for (key, _), out in zip(batch, got):
                    by_key[key] = out
                st = PROGRESS.setdefault(video_id, {"state": "working", "total": len(unique), "done": 0, "t0": time.time()})
                st["done"] = st.get("done", 0) + len(batch)

        await asyncio.gather(*[run(b) for b in batches])

    translated: Dict[int, str] = {}
    for key, idxs in mapping.items():
        zh = by_key.get(key)
        if not zh:
            continue
        for i in idxs:
            translated[i] = zh

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
        "batches": len(batches),
        "track": track,
        "_cache": "miss",
    }
    index_track(video_id, cues, translated)
    cache_put(video_id, src, tgt, result)
    PROGRESS[video_id] = {"state": "done", "total": len(unique), "done": len(unique), "t0": time.time()}
    return result


# ----------------- 逐块请求的模糊命中（A 方案：不动 App） -----------------

def index_track(video_id: str, cues: List[Tuple[int, str]], translated: Dict[int, str]) -> None:
    conn = _db()
    try:
        now = int(time.time())
        rows = []
        for idx, src in cues:
            zh = translated.get(idx)
            if not zh:
                continue
            n = normalize(src)
            if len(n) < 8:
                continue
            rows.append((video_id, idx, n, zh, now))
        conn.executemany("INSERT OR REPLACE INTO cue_index VALUES(?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()


def cue_lookup(text: str, min_score: float = 0.6) -> Optional[Dict[str, Any]]:
    """在已预翻译的整轨里找与这块文本对应的字幕条（包含匹配 + 长度比例打分）。"""
    norm = normalize(text)
    if len(norm) < 12:
        return None

    conn = _db()
    try:
        lo, hi = int(len(norm) * 0.5) - 10, int(len(norm) * 2.0) + 20
        rows = conn.execute(
            "SELECT norm, zh, video_id, idx FROM cue_index"
            " WHERE length(norm) BETWEEN ? AND ? ORDER BY ts DESC LIMIT 400",
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
            if score >= min_score and (best is None or score > best["score"]):
                best = {"score": round(score, 3), "text": zh, "video_id": video_id, "idx": idx}
    return best


def resolve_by_title(title: str) -> Optional[Dict[str, str]]:
    """按媒体会话标题在 YouTube 搜索视频（yt-dlp ytsearch1），返回 {id,title}。"""
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--skip-download",
           "--print", "%(id)s\t%(title)s", f"ytsearch1:{title}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return None
    vid, _, found = line[0].partition("\t")
    return {"id": vid.strip(), "title": found.strip()}

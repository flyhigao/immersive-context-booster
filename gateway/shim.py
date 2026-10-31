"""协议适配层：把 App 的「整块 + 要求模型逐字引述」协议，换成网关自己切句 + 编号批量翻译。

背景（2026-10 实测）：
- App（Morphe 的 RebuildProtocol）要求回复里的 source 引文**完整划分** source_text，
  多一个字符、少一条都会被判无效并重试；
- Hy-MT2-7B 对长块经常只翻前半部分，或者干脆把输入 JSON 原样抄回来 → 重试 → 一直「翻译中」；
- 网关自己切句后，引文由网关保证，模型只负责「第 N 行 → 译文」，成功率接近 100%。

不保留任何术语/语境注入，也不依赖整轨缓存。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import httpx

from gateway.config import settings

SYSTEM = (
    "You translate subtitle lines into {tgt}. You will receive numbered lines. "
    "Output exactly the same number of lines in the same order, each starting with "
    "the same number, a tab, then only the translation. Do not merge, split, add, "
    "drop or explain anything."
)

# 句末标点 + 可选空白；保持可逆（拼回去 == 原文）
_CLAUSE_RE = re.compile(r"[^.!?\u3002\uff01\uff1f]+[.!?\u3002\uff01\uff1f]*[ \t\n]*")
MIN_CLAUSE = int(os.getenv("SHIM_MIN_CLAUSE", "12"))
MAX_CLAUSE = int(os.getenv("SHIM_MAX_CLAUSE", "160"))

# 同一个 block 被重复请求（拖动进度、暂停后重播）直接秒回。
# 只做「完全相等」匹配，不做模糊/跨视频匹配，不会重现整轨方案的串行污染。
_LRU: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
LRU_MAX = int(os.getenv("SHIM_LRU_MAX", "512"))
LRU_TTL = float(os.getenv("SHIM_LRU_TTL", "1800"))


def split_clauses(text: str) -> List[str]:
    """切成「拼回去等于原文」的片段。先按句末标点，再按长度/说话人标记二次切。"""
    if not text:
        return []
    raw = _CLAUSE_RE.findall(text)
    if not raw or "".join(raw) != text:
        raw = [text]

    out: List[str] = []
    for piece in raw:
        if not piece:
            continue
        # 过长片段：在空白处切开，保持可逆
        while len(piece) > MAX_CLAUSE:
            cut = piece.rfind(" ", 0, MAX_CLAUSE)
            if cut <= 0:
                cut = MAX_CLAUSE
            out.append(piece[:cut])
            piece = piece[cut:]
        if piece:
            out.append(piece)

    # 太短的片段合并到前一个（避免把 "Yeah." 单独送翻译）
    merged: List[str] = []
    for piece in out:
        if merged and len(piece.strip()) < MIN_CLAUSE:
            merged[-1] += piece
        else:
            merged.append(piece)
    return merged


def _numbered(lines: List[str]) -> str:
    return "\n".join("{}\t{}".format(i + 1, ln.replace("\n", " ").strip()) for i, ln in enumerate(lines))


def parse_numbered(raw: str, n: int) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d{1,3})\s*[\t:\.\)]\s*(.+)$", line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < n:
                out[idx] = m.group(2).strip()
    return out


async def _ask(client: httpx.AsyncClient, lines: List[str], tgt: str) -> List[str]:
    payload = {
        "model": settings.DEFAULT_MODEL,
        "temperature": 0.2,
        "stream": False,
        "max_tokens": max(128, sum(len(x) for x in lines) * 3),
        "messages": [
            {"role": "system", "content": SYSTEM.format(tgt=tgt)},
            {"role": "user", "content": _numbered(lines)},
        ],
    }
    resp = await client.post(
        f"{settings.UPSTREAM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {settings.UPSTREAM_API_KEY}"},
        json=payload,
        timeout=120.0,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    got = parse_numbered(raw, len(lines))
    return [got.get(i, "") for i in range(len(lines))]


def _chunks(lines: List[str], limit: int) -> List[List[int]]:
    """按字符数分组，保证一次请求不会太长。"""
    out, cur, size = [], [], 0
    for i, ln in enumerate(lines):
        if cur and size + len(ln) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(ln)
    if cur:
        out.append(cur)
    return out


async def translate_block(req: Dict[str, Any], tgt: str = "zh-Hans") -> Optional[Dict[str, Any]]:
    """把一个 block 翻成 {block, events:[{source,text}]}；失败返回 None（由调用方回落到直通）。"""
    text = req.get("source_text")
    if not isinstance(text, str) or not text.strip():
        return None

    key = f"{tgt}\u0000{text}"
    cached = _LRU.get(key)
    if cached and time.time() - cached[0] < LRU_TTL:
        _LRU.move_to_end(key)
        return {"block": req.get("block", ""), "events": json.loads(cached[1]), "_cache": "hit"}

    clauses = split_clauses(text)
    if not clauses or "".join(clauses) != text:
        return None            # 切分不可逆就不冒险
    if len(clauses) == 1 and len(clauses[0]) > 400:
        return None

    limit = int(os.getenv("SHIM_BATCH_CHARS", "600"))
    results: List[str] = [""] * len(clauses)
    import asyncio

    async with httpx.AsyncClient() as client:
        async def run(idxs: List[int]) -> None:
            missing = list(idxs)
            for attempt in range(3):
                try:
                    outs = await _ask(client, [clauses[i] for i in missing], tgt)
                except Exception:
                    outs = [""] * len(missing)
                for i, zh in zip(missing, outs):
                    if zh and not results[i]:
                        results[i] = zh
                missing = [i for i in missing if not results[i]]
                if not missing:
                    return
                await asyncio.sleep(0.15 * (attempt + 1))
            # 仍有空缺的：逐条单发
            for i in missing:
                try:
                    outs = await _ask(client, [clauses[i]], tgt)
                    results[i] = outs[0] if outs else ""
                except Exception:
                    pass

        await asyncio.gather(*[run(g) for g in _chunks(clauses, limit)])

    if not all(results):
        return None
    events = [{"source": c, "text": zh} for c, zh in zip(clauses, results)]
    _LRU[key] = (time.time(), json.dumps(events, ensure_ascii=False))
    _LRU.move_to_end(key)
    while len(_LRU) > LRU_MAX:
        _LRU.popitem(last=False)
    return {"block": req.get("block", ""), "events": events}

import os
import re
import json
import asyncio
import hashlib
import time
from datetime import datetime
from collections import deque
import httpx
from typing import Optional, List, Dict, Any, Tuple
from fastapi import FastAPI, APIRouter, Request, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from gateway.config import settings
from gateway import shim
from gateway.db import (
    init_db,
    get_all_terms,
    get_term_categories,
    get_term_by_id,
    update_term_by_id,
    add_or_update_term,
    delete_term,
    find_matching_terms,
    get_active_context,
    save_context,
    get_cached_translation,
    set_cached_translation,
    is_keep_original_term
)
from gateway.analyzer import analyze_web_or_video, extract_terms_from_translation

app = FastAPI(title="Immersive Context & Term Booster Gateway")


class UpstreamHTTPError(Exception):
    """上游返回 4xx/5xx：保留原始状态码与响应体，供客户端做能力协商（如不支持 response_format 时降级）。"""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body or ""
        super().__init__(f"upstream HTTP {status_code}: {self.body[:300]}")

    def payload(self) -> Any:
        try:
            return json.loads(self.body)
        except Exception:
            return {
                "error": {
                    "message": self.body[:2000],
                    "type": "upstream_error",
                    "code": self.status_code,
                }
            }


def clean_marker_protocol_response(input_text: str, raw_text: str) -> str:
    """
    根据请求原文中实际声明的 [[p\\d+]]，严格清洗对齐大模型输出:
    1. 提取请求中所有合法的 id 列表（如 ['p0', 'p1', 'p2', 'p3']）
    2. 剥离模型开头输出的所有废话前缀，定位到第一个 [[p0]]
    3. 彻底斩断超出请求范围的脑补未知 id（如模型擅自追加的 [[p4]], [[p5]] 等），防止沉浸式翻译报 unknown_id 导致字幕堆叠回退
    4. 规范化每一行标记：确保 [[p0]] 独占一行且无空格，完美匹配沉浸式翻译严格正则 /^\[\[(p\d+)\]\]$/
    5. 确保请求中的所有 id 都存在且非空（若模型漏掉则补全为 '...'，避免 missing 校验失败）
    6. 严格规范末尾闭合标记 [[terms]]\\n[[end]]
    """
    expected_ids = re.findall(r'\[\[(p\d+)\]\]', input_text)
    if not expected_ids:
        return raw_text

    expected_set = set(expected_ids)
    raw = raw_text.replace("\r\n", "\n")
    
    first_p = re.search(r'\[\[p\d+\]\]', raw)
    if not first_p:
        return raw_text
    text = raw[first_p.start():]
    text = re.sub(r'\[\[source_end\]\]', '', text)
    
    blocks = re.split(r'(\[\[p\d+\]\])', text)
    id_map = {}
    current_id = None
    
    for part in blocks:
        if not part:
            continue
        id_match = re.fullmatch(r'\[\[(p\d+)\]\]', part.strip())
        if id_match:
            pid = id_match.group(1)
            if pid in expected_set:
                current_id = pid
                if current_id not in id_map:
                    id_map[current_id] = []
            else:
                # 遇到超出本次请求范围的脑补 id (如输入只有 p0..p3 却输出了 p4...)，立即截断停止后续收集！
                break
        else:
            if current_id and current_id in id_map:
                cleaned_text = part.strip()
                cleaned_text = re.sub(r'\[\[terms\]\].*', '', cleaned_text, flags=re.DOTALL).strip()
                cleaned_text = re.sub(r'\[\[end\]\].*', '', cleaned_text, flags=re.DOTALL).strip()
                if cleaned_text:
                    id_map[current_id].append(cleaned_text)

    cleaned_blocks = []
    for eid in expected_ids:
        cleaned_blocks.append(f"[[{eid}]]")
        texts = id_map.get(eid, [])
        if texts:
            cleaned_blocks.append("\n".join(texts))
        else:
            cleaned_blocks.append("...")

    cleaned_blocks.append("[[terms]]")
    cleaned_blocks.append("[[end]]")
    return "\n".join(cleaned_blocks)

def mask_untranslated(text: str, entities: List[str]) -> Tuple[str, Dict[str, str]]:
    """用专有占位符替换显式指定的必须保持原文字词，彻底阻断大模型将其音译为中文或替换为全名"""
    sorted_entities = sorted(entities, key=len, reverse=True)
    mapping = {}
    masked_text = text
    for i, ent in enumerate(sorted_entities):
        token = f"__P_{i}__"
        mapping[token] = ent
        masked_text = re.sub(r'\b' + re.escape(ent) + r'\b', token, masked_text)
    return masked_text, mapping

def unmask_untranslated(text: str, mapping: Dict[str, str]) -> str:
    """后置还原占位符为原始英文原词"""
    unmasked = text
    for token, ent in mapping.items():
        unmasked = unmasked.replace(token, ent)
        unmasked = unmasked.replace(token.lower(), ent)
    return unmasked

# 1. 显卡防爆异步信号量队列（控制并发为 3，多余请求平滑排队缓冲）
concurrency_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENCY)

# 2. 实时滚动日志队列（内存保留最新 200 条完整调用明细，支持前端实时查看与滚动）
live_request_logs = deque(maxlen=200)

def record_live_log(log_entry: Dict[str, Any]):
    log_entry["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    live_request_logs.appendleft(log_entry)
    # 同时异步/简单追加写入日志文件便于 tail -f
    try:
        log_file = os.path.join(os.path.dirname(settings.DATABASE_PATH), "gateway_live.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*70}\n[{log_entry['timestamp']}] {log_entry.get('type', 'REQUEST')} - Stream: {log_entry.get('stream')}\n")
            f.write(f"PROMPT / INPUT:\n{log_entry.get('user_input', '')}\n")
            f.write(f"OUTPUT (Cleaned):\n{log_entry.get('output', '')}\n")
            if log_entry.get("raw_output"):
                f.write(f"RAW MODEL OUTPUT:\n{log_entry.get('raw_output', '')}\n")
    except Exception:
        pass

# 启用 CORS，允许油猴脚本从任意网页跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 反代路径前缀示例
router = APIRouter(prefix=settings.PATH_PREFIX)

@app.on_event("startup")
def on_startup():
    init_db()
    prefix_show = settings.PATH_PREFIX or "/"
    print(f"\n========================================================")
    print(f"🚀 沉浸式翻译智能网关已启动！")
    print(f"🌐 监听地址: http://{settings.HOST}:{settings.PORT}")
    print(f"🔒 安全前缀路径: {prefix_show}")
    print(f"📘 控制台地址: http://{settings.HOST}:{settings.PORT}{settings.PATH_PREFIX}/")
    print(f"🔗 沉浸式翻译 API: http://{settings.HOST}:{settings.PORT}{settings.PATH_PREFIX}/v1/chat/completions")
    print(f"🧩 油猴脚本直装: http://{settings.HOST}:{settings.PORT}{settings.PATH_PREFIX}/immersive-context-booster.user.js")
    print(f"========================================================\n")

# ----------------- 数据模型 -----------------

class AnalyzeRequest(BaseModel):
    url: str
    title: str
    site_type: str = "web"
    content: str = ""

class TermRequest(BaseModel):
    source: str
    target: str
    category: str = "篮球/术语"
    note: str = ""
    source_url: str = ""

# ----------------- 沉浸式翻译 OpenAI 兼容代理 -----------------
@router.get("/v1/models")
@router.get("/v1/plain/models")
async def get_models():
    """提供给沉浸式翻译检测模型列表"""
    return {
        "object": "list",
        "data": [
            {"id": settings.DEFAULT_MODEL, "object": "model", "owned_by": "custom-gateway"},
            {"id": "hy1.8b", "object": "model", "owned_by": "custom-gateway"}
        ]
    }

async def _passthrough(body: Dict[str, Any], stream: bool):
    """原样转发给上游 /chat/completions，只记录耗时与 token 用量。"""
    url = f"{settings.UPSTREAM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.UPSTREAM_API_KEY}",
        "Content-Type": "application/json",
    }
    chars = 0
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            chars += sum(len(p.get("text", "")) for p in c if isinstance(p, dict))
    t0 = time.time()
    last_user = ""
    for m in reversed(body.get("messages", [])):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            last_user = m["content"]
            break
    n_sys = sum(1 for m in body.get("messages", []) if m.get("role") == "system")

    try:
        from gateway import yt_subs as _ys
        _log = _ys.log_event
    except Exception:
        _log = lambda *a, **k: None

    client = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))
    if not stream:
        try:
            resp = await client.post(url, headers=headers, json=body)
            ms = round((time.time() - t0) * 1000)
            data = resp.json() if resp.status_code == 200 else {}
            usage = data.get("usage", {})
            reply = ""
            try:
                reply = data["choices"][0]["message"]["content"][:300]
            except Exception:
                pass
            _log("PT", ms=ms, chars=chars, status=resp.status_code, sys=n_sys,
                 prompt=usage.get("prompt_tokens"), out=usage.get("completion_tokens"),
                 in_head=last_user[:100], reply=reply)
            return JSONResponse(content=data, status_code=resp.status_code)
        finally:
            await client.aclose()

    req = client.build_request("POST", url, headers=headers, json=body)
    resp = await client.send(req, stream=True)
    first = {"t": None, "buf": []}

    async def gen():
        try:
            async for chunk in resp.aiter_raw():
                if first["t"] is None:
                    first["t"] = round((time.time() - t0) * 1000)
                if len(first["buf"]) < 2000:
                    first["buf"].append(chunk)
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            head = b"".join(first["buf"])[:400].decode("utf-8", "ignore")
            _log("PT", stream=True, ms=round((time.time() - t0) * 1000),
                 ttfb=first["t"], chars=chars, status=resp.status_code, sys=n_sys,
                 in_head=last_user[:100], reply=head)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/v1/chat/completions")
@router.post("/v1/plain/chat/completions")
async def chat_completions(request: Request, background_tasks: BackgroundTasks):
    """
    核心代理入口：
    1. 本地高频翻译缓存拦截（0ms 响应，减轻 7B 显卡压力）
    2. 动态注入全局语境与单复数/屈折模糊匹配术语库
    3. 前后文滑动窗口记忆注入（理顺代词与承接语气）
    4. 显卡防爆异步信号量并发控制（排队缓冲）
    5. 双模型智能降级与后台自动术语沉淀
    """
    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    # 0. 原生协议模式（手机端 AI 字幕补丁等自带 JSON Schema 的客户端）：
    #    这类客户端在自己的 System Prompt 里已写死输出协议，任何额外注入
    #    （语境 / 术语 / “只输出译文”）都会破坏其 JSON 解析，因此整段跳过注入与缓存。
    #    进入方式：请求带 response_format，或直接把 API URL 指到 /v1/plain/chat/completions。
    is_plain_mode = (
        request.url.path.rstrip("/").endswith("/v1/plain/chat/completions")
        or bool(body.get("response_format"))
    )

    # 0.1 透明转发模式：只对手机 plain 路径生效，桌面版沉浸式翻译仍走原来的注入/缓存链路。
    #     目的：把网关当成一根钩子，只记录原版客户端真实的行为与耗时。
    if is_plain_mode and os.getenv("GATEWAY_PASSTHROUGH", "0").lower() in ("true", "1", "yes"):
        return await _passthrough(body, stream)

    # 0.2 协议适配层：网关自己切句 + 编号批量翻译，替 App 保证“引文完整划分 source_text”。
    if is_plain_mode and os.getenv("GATEWAY_SHIM", "1").lower() in ("true", "1", "yes"):
        _shim_req = None
        for m in messages:
            c = m.get("content")
            if m.get("role") == "user" and isinstance(c, str) and c.strip().startswith("{"):
                try:
                    _obj = json.loads(c)
                except Exception:
                    _obj = None
                if isinstance(_obj, dict) and isinstance(_obj.get("source_text"), str):
                    _shim_req = _obj
                break
        if _shim_req is not None and not _shim_req["source_text"].strip().lower().startswith("this is a subtitle test"):
            try:
                t0 = time.time()
                out = await shim.translate_block(_shim_req, _shim_req.get("language") or "zh-Hans")
                if out is not None:
                    try:
                        from gateway import yt_subs as _ys
                        _ys.log_event("SHIM", ms=round((time.time() - t0) * 1000),
                                      block=_shim_req.get("block"),
                                      chars=len(_shim_req["source_text"]),
                                      events=len(out["events"]),
                                      cache=out.get("_cache") == "hit",
                                      head=_shim_req["source_text"][:60])
                    except Exception:
                        pass
                    content = json.dumps(out, ensure_ascii=False)
                    if stream:
                        async def _shim_stream(c=content):
                            now = int(datetime.now().timestamp())
                            yield "data: " + json.dumps({
                                "id": f"chatcmpl-shim-{now}", "object": "chat.completion.chunk",
                                "created": now, "model": settings.DEFAULT_MODEL,
                                "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}],
                            }) + "\r\n\r\n"
                            yield "data: " + json.dumps({
                                "id": f"chatcmpl-shim-{now}", "object": "chat.completion.chunk",
                                "created": now, "model": settings.DEFAULT_MODEL,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            }) + "\r\n\r\n"
                            yield "data: [DONE]\r\n\r\n"
                        return StreamingResponse(_shim_stream(), media_type="text/event-stream")
                    return JSONResponse({
                        "id": f"chatcmpl-shim-{int(datetime.now().timestamp())}",
                        "object": "chat.completion", "model": settings.DEFAULT_MODEL,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                      "finish_reason": "stop"}],
                    })
                try:
                    from gateway import yt_subs as _ys
                    _ys.log_event("SHIM_FAIL", block=_shim_req.get("block"),
                                  chars=len(_shim_req["source_text"]))
                except Exception:
                    pass
            except Exception as e:
                print(f"[Gateway] shim failed: {e}")

    # 0. 原生协议模式（手机端 AI 字幕补丁等自带 JSON Schema 的客户端）：
    #    这类客户端在自己的 System Prompt 里已写死输出协议，任何额外注入
    #    （语境 / 术语 / “只输出译文”）都会破坏其 JSON 解析，因此整段跳过注入与缓存。
    #    进入方式：请求带 response_format，或直接把 API URL 指到 /v1/plain/chat/completions。
    is_plain_mode = (
        request.url.path.rstrip("/").endswith("/v1/plain/chat/completions")
        or bool(body.get("response_format"))
    )
    
    # 提取待翻译文本内容
    user_content = ""
    for m in messages:
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                user_content += " " + content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        user_content += " " + part.get("text", "")
    user_content_clean = user_content.strip()

    # 0.5 整轨预翻译命中（B 路线）：手机端逐块请求时，直接返回已翻好的整轨译文。
    #     只对「JSON 块请求」生效（带 source_text），且需要该视频已被 /yt/track 或 /yt/warm 预热。
    if is_plain_mode and user_content_clean.startswith("{"):
        try:
            req_obj = json.loads(user_content_clean)
        except Exception:
            req_obj = None
        if isinstance(req_obj, dict) and isinstance(req_obj.get("source_text"), str):
            try:
                from gateway import yt_subs
                hit = yt_subs.cue_lookup(req_obj["source_text"])
                if not hit:
                    # block 常常跨多条字幕，拼起来才能命中
                    hit = yt_subs.compose_lookup(req_obj["source_text"])
            except Exception as e:
                hit = None
                print(f"[Gateway] track cache lookup failed: {e}")
            _src = (req_obj.get("source_text") or "").strip().lower()
            _is_probe = _src.startswith("this is a subtitle test")
            if hit:
                try:
                    yt_subs.log_event("REQ_HIT", block=req_obj.get("block"), vid=hit.get("video_id"),
                                      idx=hit.get("idx"), score=hit.get("score"), n=len(_src))
                except Exception:
                    pass
            elif not _is_probe:
                try:
                    _hold = yt_subs.should_hold(req_obj["source_text"])
                    yt_subs.log_event("REQ_MISS", block=req_obj.get("block"), n=len(_src),
                                      hold=_hold, text=req_obj["source_text"][:80])
                except Exception:
                    _hold = False
            if (not hit and not _is_probe and _hold
                    and os.getenv("YT_HOLD_WHILE_WARMING", "true").lower() in ("true", "1", "yes")):
                try:
                    from gateway import yt_subs as _ys
                    if True:
                        content = json.dumps({"block": req_obj.get("block", ""), "events": []}, ensure_ascii=False)
                        record_live_log({"type": "TRACK_WARMING_HOLD", "stream": stream,
                                         "user_input": user_content_clean, "output": content})
                        if stream:
                            async def hold_stream():
                                now = int(datetime.now().timestamp())
                                chunk1 = {
                                    "id": f"chatcmpl-hold-{now}",
                                    "object": "chat.completion.chunk",
                                    "created": now,
                                    "model": settings.DEFAULT_MODEL,
                                    "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                                }
                                chunk2 = {
                                    "id": f"chatcmpl-hold-{now}",
                                    "object": "chat.completion.chunk",
                                    "created": now,
                                    "model": settings.DEFAULT_MODEL,
                                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                }
                                yield "data: " + json.dumps(chunk1) + "\r\n\r\n"
                                yield "data: " + json.dumps(chunk2) + "\r\n\r\n"
                                yield "data: [DONE]\r\n\r\n"
                            return StreamingResponse(hold_stream(), media_type="text/event-stream")
                        return JSONResponse({
                            "id": f"chatcmpl-hold-{int(datetime.now().timestamp())}",
                            "object": "chat.completion",
                            "model": settings.DEFAULT_MODEL,
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                          "finish_reason": "stop"}],
                        })
                except Exception as e:
                    print(f"[Gateway] warm-hold check failed: {e}")
            if hit:
                content = json.dumps({
                    "block": req_obj.get("block", ""),
                    "events": [{"source": req_obj["source_text"], "text": hit["text"]}],
                }, ensure_ascii=False)
                record_live_log({
                    "type": "TRACK_CACHE_HIT",
                    "stream": stream,
                    "user_input": user_content_clean,
                    "output": content,
                })
                if stream:
                    async def track_stream():
                        now = int(datetime.now().timestamp())
                        yield "data: " + json.dumps({
                            "id": f"chatcmpl-track-{now}", "object": "chat.completion.chunk",
                            "created": now, "model": settings.DEFAULT_MODEL,
                            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                        }) + "\r\n\r\n"
                        yield "data: " + json.dumps({
                            "id": f"chatcmpl-track-{now}", "object": "chat.completion.chunk",
                            "created": now, "model": settings.DEFAULT_MODEL,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        }) + "\r\n\r\n"
                        yield "data: [DONE]\r\n\r\n"
                    return StreamingResponse(track_stream(), media_type="text/event-stream")
                return JSONResponse({
                    "id": f"chatcmpl-track-{int(datetime.now().timestamp())}",
                    "object": "chat.completion",
                    "model": settings.DEFAULT_MODEL,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                  "finish_reason": "stop"}],
                })

    # 1. 查询当前活跃网页/视频全局上下文
    active_ctx = get_active_context()

    # 1.5 走到这里说明整轨缓存没命中：要真的调模型了（慢路径，必须记录）
    if is_plain_mode and user_content_clean.startswith("{"):
        try:
            from gateway import yt_subs as _ys2
            _ys2.log_event("REQ_MODEL", n=len(user_content_clean))
            record_live_log({"type": "TRACK_MODEL_FALLBACK", "stream": stream,
                             "user_input": user_content_clean[:400], "output": ""})
        except Exception:
            pass

    # 2. 检查本地高频缓存 (Cache)
    cache_key = ""
    if settings.ENABLE_CACHE and user_content_clean and not is_plain_mode:
        ctx_topic = active_ctx.get("topic", "") if active_ctx else ""
        cache_raw = f"{settings.DEFAULT_MODEL}:{ctx_topic}:{user_content_clean}"
        cache_key = hashlib.md5(cache_raw.encode("utf-8")).hexdigest()
        cached_result = get_cached_translation(cache_key)
        
        if cached_result:
            record_live_log({
                "type": "CACHE_HIT",
                "stream": stream,
                "user_input": user_content_clean,
                "output": cached_result
            })
            if stream:
                async def cached_stream():
                    chunk_id = f"chatcmpl-cache-{cache_key[:8]}"
                    first_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": 1700000000,
                        "model": settings.DEFAULT_MODEL,
                        "choices": [{"index": 0, "delta": {"content": cached_result}, "finish_reason": None}]
                    }
                    stop_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": 1700000000,
                        "model": settings.DEFAULT_MODEL,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                    }
                    yield f"data: {json.dumps(first_chunk)}\r\n\r\n"
                    yield f"data: {json.dumps(stop_chunk)}\r\n\r\n"
                    yield "data: [DONE]\r\n\r\n"
                return StreamingResponse(cached_stream(), media_type="text/event-stream")
            else:
                return JSONResponse({
                    "id": f"chatcmpl-cache-{cache_key[:8]}",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": settings.DEFAULT_MODEL,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": cached_result},
                        "finish_reason": "stop"
                    }]
                })

    # 3. 术语库智能屈折变化模糊匹配（原生协议模式跳过）
    matched_terms = [] if is_plain_mode else find_matching_terms(user_content)
    
    keep_original_terms = [t for t in matched_terms if is_keep_original_term(t["source"], t["target"])]
    translate_terms = [t for t in matched_terms if not is_keep_original_term(t["source"], t["target"])]
    user_translate_sources = {t["source"].lower() for t in translate_terms}

    # 4. 提取需进行无损占位符保护的实体列表（针对显式指定的保持原词术语，如 Yaxel, Yax 等）
    entities_to_protect = [t["source"].strip() for t in keep_original_terms]

    # 对输入消息执行前置占位保护（彻底阻断模型将特定词音译或替换为全名）
    entity_mapping = {}
    if entities_to_protect:
        for m in messages:
            if m.get("role") == "user":
                c_text = m.get("content", "")
                if isinstance(c_text, str):
                    masked_c, m_map = mask_untranslated(c_text, entities_to_protect)
                    m["content"] = masked_c
                    entity_mapping.update(m_map)

    # 5. 组装 System 提示词增强块（精简强力，杜绝 7B 模型复读与幻觉）
    injection_parts = []
    
    # 英文人名与球员姓名强制保留英文原文铁律
    if settings.KEEP_NAMES_UNTRANSLATED and not is_plain_mode:
        injection_parts.append(
            "【重要翻译规则 - 英文人名强制保留英文原文】：\n"
            "在翻译中，所有出现的西方人名、球星名字、球员简称（例如 Al Horford, Steph, Curry, Klay, LeBron, Luka, Yaxel 等），在中文译文中必须统一 100% 原样保留英文，严禁音译为中文汉字（例如：Al Horford 绝不能翻译为“霍福德”或“阿尔·霍福德”，必须直接写作 Al Horford；Steph 绝不能翻译为“斯蒂芬”，必须直接写作 Steph；Yaxel 必须直接写作 Yaxel；Curry 必须直接写作 Curry）。严禁将任何人名翻译为中文。"
        )

    if entity_mapping:
        injection_parts.append(
            "【专有代号保护规则】：文本中出现的形如 __P_0__, __P_1__ 为受保护的专有代号，在中文译文中必须原样保留该代号不变。"
        )
    if active_ctx and active_ctx.get("topic") and not is_plain_mode:
        topic_clean = active_ctx.get("topic", "").strip()
        is_context_relevant = True
        domain_str = active_ctx.get("domain", "")
        if "篮球" in domain_str or "warriors" in topic_clean.lower() or "nba" in topic_clean.lower():
            basketball_keywords = ["nba", "ball", "shot", "game", "point", "court", "coach", "play", "team", "score", "foul", "dunk", "rebound", "assist", "quarter", "lead", "win", "lose", "warriors", "lakers", "celtics", "curry", "lebron"]
            if not any(k in user_content.lower() for k in basketball_keywords):
                is_context_relevant = False
                
        if is_context_relevant and topic_clean:
            injection_parts.append(
                f"【语境参考】：当前正在阅读关于《{topic_clean}》的内容，请保持对应领域的专业用词与地道风格。"
            )

    # 术语提示：仅注入需要翻译的专业术语映射表
    if translate_terms:
        term_rules = []
        for t in translate_terms[:25]:
            note_str = f" ({t['note']})" if t.get("note") else ""
            term_rules.append(f"  * \"{t['source']}\" -> \"{t['target']}\"{note_str}")
        term_text = "\n".join(term_rules)
        injection_parts.append(
            f"【专业术语映射表（遇到对应词汇请准确对照翻译）】：\n{term_text}"
        )

    # 6. 把注入块插入到 messages 的首位 System Prompt 中
    if injection_parts:
        injection_content = "\n\n".join(injection_parts) + "\n\n请严格只输出对应中文翻译结果，严禁输出任何多余解释、前缀说明或未要求的符号。"
        system_idx = -1
        for idx, m in enumerate(messages):
            if m.get("role") == "system":
                system_idx = idx
                break
                
        if system_idx != -1:
            messages[system_idx]["content"] = (
                messages[system_idx]["content"] + "\n\n" + injection_content
            )
        else:
            messages.insert(0, {
                "role": "system",
                "content": "你是一个资深精通特定领域文化的专业翻译专家。\n\n" + injection_content
            })

    # 7. 构造转发载荷
    upstream_payload = {
        **body,
        "model": settings.DEFAULT_MODEL,
        "messages": messages,
        "temperature": body.get("temperature", 0.3)
    }

    headers = {
        "Authorization": f"Bearer {settings.UPSTREAM_API_KEY}",
        "Content-Type": "application/json"
    }

    # 辅助函数：双模型平滑路由（优先主模型，异常自动切备用）
    async def call_llm_post(client, payload):
        try:
            resp = await client.post(
                f"{settings.UPSTREAM_BASE_URL}/chat/completions",
                headers=headers,
                json=payload
            )
            if resp.status_code >= 400:
                # 上游 4xx/5xx 原样上抛：手机端补丁（AI caption translator 等）
                # 依赖错误文本判断“response_format 不支持”并自动降级，
                # 不能只包成 502，否则协议协商会失败。
                raise UpstreamHTTPError(resp.status_code, resp.text)
            return resp.json()
        except UpstreamHTTPError:
            raise
        except Exception as e:
            if settings.BACKUP_BASE_URL and settings.BACKUP_API_KEY:
                print(f"[Gateway] 上游模型异常，自动切换到备用模型通道: {e}")
                backup_headers = {
                    "Authorization": f"Bearer {settings.BACKUP_API_KEY}",
                    "Content-Type": "application/json"
                }
                backup_payload = {**payload, "model": settings.BACKUP_MODEL or payload.get("model")}
                backup_resp = await client.post(
                    f"{settings.BACKUP_BASE_URL}/chat/completions",
                    headers=backup_headers,
                    json=backup_payload
                )
                backup_resp.raise_for_status()
                return backup_resp.json()
            raise e

    # 记录译文到本地缓存
    def record_translation_result(src: str, tgt: str):
        if not src or not tgt:
            return
        # 异常输出过滤：如果输出包含异常的长串 html 闭合标签，不存入缓存
        if "span></a><div" in tgt or "正在浏览：" in tgt:
            return
        if settings.ENABLE_CACHE and cache_key:
            set_cached_translation(cache_key, src.strip(), tgt.strip(), settings.DEFAULT_MODEL)

    client = httpx.AsyncClient(timeout=60.0)

    # 7. 流式响应处理（受并发信号量保护）
    if stream:
        async def stream_generator():
            accumulated_response = ""
            is_marker_mode = "[[p" in user_content_clean

            async with concurrency_semaphore:
                try:
                    async with client.stream(
                        "POST",
                        f"{settings.UPSTREAM_BASE_URL}/chat/completions",
                        headers=headers,
                        json=upstream_payload
                    ) as upstream_resp:
                        if not is_marker_mode:
                            # 普通网页翻译：实时边译边流式输出
                            async for line in upstream_resp.aiter_lines():
                                if not line:
                                    continue
                                yield f"{line}\r\n\r\n"
                                if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                                    try:
                                        chunk = json.loads(line[6:])
                                        delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                        if delta:
                                            accumulated_response += delta
                                    except:
                                        pass
                        else:
                            # YouTube 字幕与段落批量模式：
                            # 先完整接收模型输出，严格斩断脑补的未知 id (如 p4..)，再输出合规流，彻底解决多行字幕堆叠！
                            async for line in upstream_resp.aiter_lines():
                                if not line:
                                    continue
                                if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                                    try:
                                        chunk = json.loads(line[6:])
                                        delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                        if delta:
                                            accumulated_response += delta
                                    except:
                                        pass

                            # 模型流结束，执行严格协议清洗、超出 ID 截断，并还原受保护的英文人名
                            final_cleaned = clean_marker_protocol_response(user_content_clean, accumulated_response)
                            if entity_mapping:
                                final_cleaned = unmask_untranslated(final_cleaned, entity_mapping)
                            now_ts = int(datetime.now().timestamp())
                            clean_chunk = {
                                "id": f"chatcmpl-sub-{now_ts}",
                                "object": "chat.completion.chunk",
                                "created": now_ts,
                                "model": settings.DEFAULT_MODEL,
                                "choices": [{"index": 0, "delta": {"content": final_cleaned}, "finish_reason": None}]
                            }
                            stop_chunk = {
                                "id": f"chatcmpl-sub-{now_ts}",
                                "object": "chat.completion.chunk",
                                "created": now_ts,
                                "model": settings.DEFAULT_MODEL,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                            }
                            yield f"data: {json.dumps(clean_chunk)}\r\n\r\n"
                            yield f"data: {json.dumps(stop_chunk)}\r\n\r\n"
                            yield "data: [DONE]\r\n\r\n"
                finally:
                    await client.aclose()
                    if user_content_clean and accumulated_response.strip():
                        final_res = clean_marker_protocol_response(user_content_clean, accumulated_response) if is_marker_mode else accumulated_response
                        if entity_mapping:
                            final_res = unmask_untranslated(final_res, entity_mapping)
                        record_live_log({
                            "type": "MODEL_STREAM",
                            "stream": True,
                            "user_input": user_content_clean,
                            "raw_output": accumulated_response if accumulated_response != final_res else None,
                            "output": final_res
                        })
                        record_translation_result(user_content_clean, final_res)
                        if settings.AUTO_EXTRACT_TERMS:
                            background_tasks.add_task(
                                extract_terms_from_translation,
                                user_content_clean,
                                final_res,
                                active_ctx.get("domain", "") if active_ctx else ""
                            )

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    # 8. 非流式响应处理（受并发信号量保护）
    else:
        async with concurrency_semaphore:
            try:
                data = await call_llm_post(client, upstream_payload)
                await client.aclose()
                
                translated_content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                raw_before_clean = translated_content
                if user_content_clean and translated_content.strip():
                    if "[[p" in user_content_clean:
                        translated_content = clean_marker_protocol_response(user_content_clean, translated_content)
                    if entity_mapping:
                        translated_content = unmask_untranslated(translated_content, entity_mapping)
                    data["choices"][0]["message"]["content"] = translated_content

                    record_live_log({
                        "type": "MODEL_NON_STREAM",
                        "stream": False,
                        "user_input": user_content_clean,
                        "raw_output": raw_before_clean if raw_before_clean != translated_content else None,
                        "output": translated_content
                    })
                    record_translation_result(user_content_clean, translated_content)
                    if settings.AUTO_EXTRACT_TERMS:
                        background_tasks.add_task(
                            extract_terms_from_translation,
                            user_content_clean,
                            translated_content,
                            active_ctx.get("domain", "") if active_ctx else ""
                        )
                return JSONResponse(data)
            except UpstreamHTTPError as e:
                await client.aclose()
                return JSONResponse(status_code=e.status_code, content=e.payload())
            except Exception as e:
                await client.aclose()
                raise HTTPException(status_code=502, detail=f"上游模型请求失败: {str(e)}")

# ----------------- 网页 / 视频上下文分析 API -----------------

@router.post("/api/analyze")
async def api_analyze(req: AnalyzeRequest):
    result = await analyze_web_or_video(
        url=req.url,
        title=req.title,
        site_type=req.site_type,
        content=req.content
    )
    return {"status": "ok", "context": result}

@router.get("/api/context/current")
async def api_get_current_context():
    ctx = get_active_context()
    return {"status": "ok", "context": ctx}

@router.post("/api/context/clear")
async def api_clear_context():
    save_context(
        url="",
        title="通用模式",
        site_type="general",
        domain="通用",
        topic="",
        style="标准",
        summary=""
    )
    return {"status": "ok", "message": "当前语境已重置为通用模式"}

# ----------------- 术语库管理 API -----------------

@router.get("/api/terms")
async def api_get_terms(search: Optional[str] = None, category: Optional[str] = None, limit: int = 100, offset: int = 0):
    terms, total = get_all_terms(search=search, category=category, limit=limit, offset=offset)
    return {"status": "ok", "total": total, "terms": terms}

@router.get("/api/terms/categories")
async def api_get_categories():
    cats = get_term_categories()
    return {"status": "ok", "categories": cats}

@router.post("/api/terms")
async def api_add_term(req: TermRequest):
    row = add_or_update_term(
        source=req.source,
        target=req.target,
        category=req.category,
        note=req.note,
        source_url=req.source_url
    )
    return {"status": "ok", "term": row}

@router.get("/api/terms/{term_id}")
async def api_get_term(term_id: int):
    term = get_term_by_id(term_id)
    if not term:
        raise HTTPException(status_code=404, detail="术语不存在")
    return {"status": "ok", "term": term}

@router.put("/api/terms/{term_id}")
async def api_update_term(term_id: int, req: TermRequest):
    row = update_term_by_id(
        term_id=term_id,
        source=req.source,
        target=req.target,
        category=req.category,
        note=req.note
    )
    if not row:
        raise HTTPException(status_code=404, detail="术语不存在或未修改")
    return {"status": "ok", "term": row}

@router.delete("/api/terms/{term_id}")
async def api_delete_term(term_id: int):
    success = delete_term(term_id)
    if not success:
        raise HTTPException(status_code=404, detail="术语不存在")
    return {"status": "ok", "message": "删除成功"}

@router.get("/api/terms/export")
async def api_export_terms():
    terms, _ = get_all_terms(limit=10000)
    return JSONResponse(terms)

# ----------------- 整轨字幕预翻译 API（B 路线服务端） -----------------

@router.get("/yt/track")
async def yt_track(v: str, src: str = "en", tgt: str = "zh-Hans"):
    """返回整条字幕轨的 json3（已译为 tgt）。首次请求会现拉+现翻，之后走缓存。"""
    from gateway import yt_subs
    started = time.time()
    result = await yt_subs.translate_track(v, src, tgt)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return JSONResponse(
        content=result["track"],
        headers={
            "X-YT-Cues": str(result.get("cues", 0)),
            "X-YT-Batches": str(result.get("batches", 0)),
            "X-YT-Elapsed": f"{time.time() - started:.2f}",
        },
    )


@router.get("/yt/status")
async def yt_status(v: str):
    from gateway import yt_subs
    vid = yt_subs.video_id_from(v) or v
    return {"video_id": vid, **yt_subs.progress(vid)}


@router.get("/yt/debug")
async def yt_debug(limit: int = 40):
    """实时查看：当前激活视频、各视频预热状态、最近调试日志。"""
    from gateway import yt_subs

    tail: List[str] = []
    try:
        with open(yt_subs.LOG_PATH, encoding="utf-8") as fh:
            tail = fh.read().splitlines()[-limit:]
    except Exception:
        pass
    return {
        "active": yt_subs.ACTIVE,
        "window_seconds": yt_subs.WINDOW_SECONDS,
        "proxy": yt_subs.YT_PROXY or None,
        "progress": {k: yt_subs.progress(k) for k in list(yt_subs.PROGRESS)[-10:]},
        "log": tail,
    }


@router.get("/yt/debug/clear")
async def yt_debug_clear():
    from gateway import yt_subs
    try:
        open(yt_subs.LOG_PATH, "w", encoding="utf-8").close()
    except Exception:
        pass
    return {"status": "ok"}


@router.get("/yt/debug/summary")
async def yt_debug_summary(lines: int = 5000):
    """统计最近日志：命中/hold/调模型各多少次、各耗时多少。"""
    from gateway import yt_subs

    counts: Dict[str, int] = {}
    batch_secs: List[float] = []
    try:
        with open(yt_subs.LOG_PATH, encoding="utf-8") as fh:
            raw = fh.read().splitlines()[-lines:]
    except Exception:
        raw = []
    for ln in raw:
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        ev = rec.get("event", "?")
        counts[ev] = counts.get(ev, 0) + 1
        if ev == "BATCH" and rec.get("secs"):
            batch_secs.append(float(rec["secs"]))
    total_req = counts.get("REQ_HIT", 0) + counts.get("REQ_MISS", 0)
    held = counts.get("REQ_MISS", 0)
    return {
        "counts": counts,
        "requests": total_req,
        "model_calls": counts.get("REQ_MODEL", 0),
        "batches": len(batch_secs),
        "batch_secs_avg": round(sum(batch_secs) / len(batch_secs), 2) if batch_secs else 0,
        "batch_secs_max": round(max(batch_secs), 2) if batch_secs else 0,
    }


@router.get("/yt/", response_class=HTMLResponse)
async def yt_page():
    """手机端预热页：粘贴视频链接 → 后台整轨翻译 → 显示进度。"""
    return HTMLResponse("""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>字幕预热</title>
<style>
 body{font-family:system-ui;margin:0;padding:20px;background:#111;color:#eee}
 h2{font-weight:600;margin:0 0 16px}
 input{width:100%;box-sizing:border-box;padding:14px;font-size:16px;border-radius:10px;border:1px solid #333;background:#1b1b1b;color:#eee}
 button{margin-top:12px;width:100%;padding:14px;font-size:17px;border:0;border-radius:10px;background:#2f6fdb;color:#fff}
 #bar{margin-top:20px;height:14px;background:#222;border-radius:8px;overflow:hidden}
 #fill{height:100%;width:0;background:#39c07a;transition:width .4s}
 #txt{margin-top:10px;font-size:15px;color:#9fb3c8}
</style></head><body>
<h2>整轨字幕预热</h2>
<input id="u" placeholder="粘贴 YouTube 链接或视频 ID" />
<button onclick="go()">开始翻译</button>
<div id="bar"><div id="fill"></div></div>
<div id="txt">等待输入</div>
<script>
let timer=null;
async function go(){
  const v=document.getElementById('u').value.trim();
  if(!v) return;
  document.getElementById('txt').textContent='正在预热…';
  const r=await fetch('warm?v='+encodeURIComponent(v),{method:'POST'});
  const j=await r.json();
  poll(j.video_id||v);
}
function poll(v){
  if(timer) clearInterval(timer);
  timer=setInterval(async()=>{
    const s=await (await fetch('status?v='+encodeURIComponent(v))).json();
    if(s.state==='done'){document.getElementById('fill').style.width='100%';
      document.getElementById('txt').textContent='翻译完成，共 '+(s.total||'?')+' 条，用时 '+(s.elapsed||0)+'s';
      clearInterval(timer);return;}
    if(s.state==='unknown'){document.getElementById('txt').textContent='未找到任务';clearInterval(timer);return;}
    document.getElementById('fill').style.width=(s.percent||0)+'%';
    document.getElementById('txt').textContent='翻译中… '+(s.done||0)+'/'+(s.total||0)+' 条（'+(s.percent||0)+'%）';
  },1000);
}
</script></body></html>""")


@router.get("/yt/resolve")
async def yt_resolve(title: str, src: str = "en", tgt: str = "zh-Hans"):
    """按媒体会话标题解析视频并后台预热（给 watch-and-warm 脚本用）。"""
    from gateway import yt_subs
    info = await asyncio.to_thread(yt_subs.resolve_by_title, title)
    if not info:
        raise HTTPException(status_code=404, detail="not_found")
    asyncio.create_task(yt_subs.translate_track(info["id"], src, tgt))
    return {"status": "ok", **info}


@router.get("/yt/warm")
@router.post("/yt/warm")
async def yt_warm(v: str, src: str = "en", tgt: str = "zh-Hans", background_tasks: BackgroundTasks = None):
    """后台预热整条字幕轨（v 可以是链接或 11 位 ID；返回立即）。"""
    from gateway import yt_subs

    vid = yt_subs.video_id_from(v)
    if not vid:
        raise HTTPException(status_code=400, detail="invalid_video")

    # 协议适配层模式下不需要整轨预热（会白占模型 slot，反而拖慢逐块翻译）
    if os.getenv("GATEWAY_SHIM", "1").lower() in ("true", "1", "yes"):
        return {"status": "disabled", "video_id": vid, "reason": "shim_mode"}

    # 当前在看的视频：hold 只针对这个视频，其他视频预热不会把字幕卡成“翻译中”
    yt_subs.set_active(vid)

    cur = yt_subs.PROGRESS.get(vid)
    if cur and cur.get("state") in ("queued", "fetching", "working"):
        yt_subs.log_event("WARM_DUP", vid=vid, state=cur.get("state"))
        return {"status": "already", "video_id": vid}

    yt_subs.PROGRESS[vid] = {"state": "queued", "total": 0, "done": 0, "t0": time.time()}
    yt_subs.log_event("WARM_START", vid=vid, src=src, tgt=tgt)

    async def _run():
        await yt_subs.translate_track(vid, src, tgt)

    if background_tasks is not None:
        background_tasks.add_task(_run)
    else:
        asyncio.create_task(_run())
    return {"status": "ok", "video_id": vid, "src": src, "tgt": tgt}


# ----------------- 实时滚动日志 API -----------------

@router.get("/api/logs/live")
async def api_get_live_logs():
    return {"status": "ok", "logs": list(live_request_logs)}

@router.post("/api/logs/clear")
async def api_clear_live_logs():
    live_request_logs.clear()
    return {"status": "ok", "message": "实时日志已清空"}

# ----------------- 油猴脚本直装入口 -----------------

USERSCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "userscript",
    "immersive-context-booster.user.js"
)

@router.get("/immersive-context-booster.user.js")
async def serve_userscript(request: Request):
    """
    提供给 Tampermonkey 一键识别并安装更新的脚本源文件
    动态适配当前访问的主机名与安全前缀
    """
    if not os.path.exists(USERSCRIPT_PATH):
        raise HTTPException(status_code=404, detail="脚本文件不存在")
        
    with open(USERSCRIPT_PATH, "r", encoding="utf-8") as f:
        script_content = f.read()

    # 动态将脚本内的 GATEWAY_URL 与 @connect 替换为客户端当前访问的实际 URL
    host_header = request.headers.get("x-forwarded-host") or request.headers.get("host", f"127.0.0.1:{settings.PORT}")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    if "https" in scheme or not host_header.startswith("127.") and not host_header.startswith("192.") and not host_header.startswith("10.") and not host_header.startswith("172."):
        scheme = "https"
    current_gateway = f"{scheme}://{host_header}{settings.PATH_PREFIX}"
    domain_only = host_header.split(":")[0]

    # 替换脚本头中的 connect 权限、updateURL / downloadURL 以及代码里的 GATEWAY_URL
    script_content = script_content.replace(
        '// @connect      *',
        f'// @connect      {domain_only}\n// @connect      127.0.0.1\n// @connect      *'
    )
    script_content = script_content.replace(
        'const GATEWAY_URL = "http://127.0.0.1:8787";',
        f'const GATEWAY_URL = "{current_gateway}";'
    )
    script_content = script_content.replace(
        '// @updateURL    http://127.0.0.1:8787/immersive-context-booster.user.js',
        f'// @updateURL    {current_gateway}/immersive-context-booster.user.js'
    )
    script_content = script_content.replace(
        '// @downloadURL  http://127.0.0.1:8787/immersive-context-booster.user.js',
        f'// @downloadURL  {current_gateway}/immersive-context-booster.user.js'
    )

    return StreamingResponse(
        iter([script_content]),
        media_type="application/javascript",
        headers={"Content-Disposition": "inline; filename=immersive-context-booster.user.js"}
    )

# ----------------- 静态 Web 管理页面 -----------------

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

@router.get("/")
async def index_page():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@router.get("/static/{file_path:path}")
async def static_files(file_path: str):
    full_path = os.path.join(STATIC_DIR, file_path)
    if os.path.exists(full_path):
        return FileResponse(full_path)
    raise HTTPException(status_code=404, detail="File not found")

# 将 Router 挂载到主应用
app.include_router(router)

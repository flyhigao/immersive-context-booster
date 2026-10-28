import os
import re
import json
import asyncio
import hashlib
from collections import deque
import httpx
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, APIRouter, Request, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from gateway.config import settings
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
    set_cached_translation
)
from gateway.analyzer import analyze_web_or_video, extract_terms_from_translation

app = FastAPI(title="Immersive Context & Term Booster Gateway")

def clean_marker_protocol_response(raw_text: str) -> str:
    """
    清洗大模型输出，严格保证符合沉浸式翻译批量标记协议 (Marker Protocol):
    1. 剥离大模型开头擅自输出的前缀（如 "to Simplified Chinese Language:", "以下是翻译：" 等）
    2. 将第一处 [[p\d+]] 作为绝对起点，防止沉浸式翻译解析器因为前置杂质报 extra_content 错误
    3. 剔除末尾大模型复读的 [[source_end]]
    4. 规范化每一行标记：确保 [[p0]] 独占一行且无空格，完美满足沉浸式翻译的正则是严格整行匹配 /^\[\[(p\d+)\]\]$/
    5. 补齐规范的 [[terms]]\\n[[end]]，保证沉浸式翻译将各句字幕精准切分到对应的时间轴，避免整批字幕堆在同一画面
    """
    if "[[p" not in raw_text:
        return raw_text
        
    text = raw_text.replace("\r\n", "\n")
    # 找到第一个 [[p\d+]] 的位置，剥离之前的所有杂质前缀
    match = re.search(r'\[\[p\d+\]\]', text)
    if match:
        text = text[match.start():]
        
    # 移除末尾大模型可能复读的 [[source_end]]
    text = re.sub(r'\[\[source_end\]\]', '', text).strip()
    
    # 规范化每一行标记：确保 [[p0]] 独占一行且无后置空格
    text = re.sub(r'\[\[(p\d+)\]\][ \t]*\n?', r'[[\1]]\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    # 补齐沉浸式翻译期望的闭合标记
    if "[[end]]" not in text:
        if "[[terms]]" not in text:
            text += "\n[[terms]]"
        text += "\n[[end]]"
        
    return text

# 1. 显卡防爆异步信号量队列（控制并发为 3，多余请求平滑排队缓冲）
concurrency_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENCY)

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
async def get_models():
    """提供给沉浸式翻译检测模型列表"""
    return {
        "object": "list",
        "data": [
            {"id": settings.DEFAULT_MODEL, "object": "model", "owned_by": "custom-gateway"},
            {"id": "hy1.8b", "object": "model", "owned_by": "custom-gateway"}
        ]
    }

@router.post("/v1/chat/completions")
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

    # 1. 查询当前活跃网页/视频全局上下文
    active_ctx = get_active_context()

    # 2. 检查本地高频缓存 (Cache)
    cache_key = ""
    if settings.ENABLE_CACHE and user_content_clean:
        ctx_topic = active_ctx.get("topic", "") if active_ctx else ""
        cache_raw = f"{settings.DEFAULT_MODEL}:{ctx_topic}:{user_content_clean}"
        cache_key = hashlib.md5(cache_raw.encode("utf-8")).hexdigest()
        cached_result = get_cached_translation(cache_key)
        
        if cached_result:
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

    # 3. 术语库智能屈折变化模糊匹配
    matched_terms = find_matching_terms(user_content)
    
    # 4. 组装 System 提示词增强块（精简强力，杜绝 7B 模型复读与幻觉）
    injection_parts = []
    
    # 语境提示：仅在有效且主题明确时注入，单行极简
    if active_ctx and active_ctx.get("topic"):
        topic_clean = active_ctx.get("topic", "").strip()
        # 基础相关性校验：若语境是篮球论坛，但文本明显完全不相关，则不硬塞
        is_context_relevant = True
        domain_str = active_ctx.get("domain", "")
        if "篮球" in domain_str or "warriors" in topic_clean.lower() or "nba" in topic_clean.lower():
            # 只有文本包含基本体育/篮球/比赛词汇时才视作相关
            basketball_keywords = ["nba", "ball", "shot", "game", "point", "court", "coach", "play", "team", "score", "foul", "dunk", "rebound", "assist", "quarter", "lead", "win", "lose", "warriors", "lakers", "celtics", "curry", "lebron"]
            if not any(k in user_content.lower() for k in basketball_keywords):
                is_context_relevant = False
                
        if is_context_relevant and topic_clean:
            injection_parts.append(
                f"【语境参考】：当前正在阅读关于《{topic_clean}》的内容，请保持对应领域的专业用词与地道风格。"
            )

    # 术语提示：仅当当前段落明确命中专有词时注入具体映射，绝不多说废话
    if matched_terms:
        term_rules = []
        for t in matched_terms[:20]:
            term_rules.append(f"  * \"{t['source']}\" -> \"{t['target']}\"")
        term_text = "\n".join(term_rules)
        injection_parts.append(
            f"【专业术语映射表（遇到对应词汇请准确对照翻译）】：\n{term_text}"
        )

    # 5. 把注入块插入到 messages 的首位 System Prompt 中
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

    # 6. 构造转发载荷
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
            resp.raise_for_status()
            return resp.json()
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
            header_discarded = not is_marker_mode
            buffer_header = ""

            async with concurrency_semaphore:
                try:
                    async with client.stream(
                        "POST",
                        f"{settings.UPSTREAM_BASE_URL}/chat/completions",
                        headers=headers,
                        json=upstream_payload
                    ) as upstream_resp:
                        async for line in upstream_resp.aiter_lines():
                            if not line:
                                continue
                            if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                                try:
                                    chunk = json.loads(line[6:])
                                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                    if delta:
                                        accumulated_response += delta
                                        
                                        # 如果是字幕/段落批处理模式且尚未丢弃模型前缀
                                        if is_marker_mode and not header_discarded:
                                            buffer_header += delta
                                            p_match = re.search(r'\[\[p\d+\]\]', buffer_header)
                                            if p_match:
                                                header_discarded = True
                                                # 仅发送从 [[p 开始的部分
                                                clean_delta = buffer_header[p_match.start():]
                                                chunk["choices"][0]["delta"]["content"] = clean_delta
                                                yield f"data: {json.dumps(chunk)}\r\n\r\n"
                                            else:
                                                # 前缀尚未匹配到 [[p，先不发送，等待匹配
                                                continue
                                        else:
                                            yield f"{line}\r\n\r\n"
                                except:
                                    yield f"{line}\r\n\r\n"
                            else:
                                # 处理流结束前的闭合标记补齐
                                if is_marker_mode and line.startswith("data: [DONE]"):
                                    if "[[end]]" not in accumulated_response:
                                        tail_text = "\n[[terms]]\n[[end]]"
                                        tail_chunk = {
                                            "id": "chatcmpl-tail",
                                            "object": "chat.completion.chunk",
                                            "created": 1700000000,
                                            "model": settings.DEFAULT_MODEL,
                                            "choices": [{"index": 0, "delta": {"content": tail_text}, "finish_reason": None}]
                                        }
                                        yield f"data: {json.dumps(tail_chunk)}\r\n\r\n"
                                yield f"{line}\r\n\r\n"
                finally:
                    await client.aclose()
                    if user_content_clean and accumulated_response.strip():
                        final_cleaned = clean_marker_protocol_response(accumulated_response) if is_marker_mode else accumulated_response
                        record_translation_result(user_content_clean, final_cleaned)
                        if settings.AUTO_EXTRACT_TERMS:
                            background_tasks.add_task(
                                extract_terms_from_translation,
                                user_content_clean,
                                final_cleaned,
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
                if user_content_clean and translated_content.strip():
                    if "[[p" in user_content_clean:
                        translated_content = clean_marker_protocol_response(translated_content)
                        data["choices"][0]["message"]["content"] = translated_content

                    record_translation_result(user_content_clean, translated_content)
                    if settings.AUTO_EXTRACT_TERMS:
                        background_tasks.add_task(
                            extract_terms_from_translation,
                            user_content_clean,
                            translated_content,
                            active_ctx.get("domain", "") if active_ctx else ""
                        )
                return JSONResponse(data)
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

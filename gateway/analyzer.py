import json
import re
import httpx
from typing import Dict, List, Optional
from gateway.config import settings
from gateway.db import save_context, add_or_update_term, is_valid_term

ANALYSIS_SYSTEM_PROMPT = """你是一个专业的双语语境与术语分析专家。
你的任务是在用户阅读或观看网页/问答/视频前，分析整篇内容的全局语境，并提取专有术语（特别是篮球NBA、Reddit社区俚语、Quora专业回答/叙事风格、战术黑话、文化梗、行业专有名词），以便为后续网页双语翻译提供准确指导。

针对 Quora 等综合问答社区：
- 识别具体问题领域（如：技术工程、医学健康、历史政治、个人情感叙事、职场职级等）；
- 识别答主的专业背景或人设资质（如答主是专业学者还是普通读者）；
- 识别行文风格与语气（如：严谨学术、幽默反讽、真诚口语、感性叙述），以便后续翻译时保持中文语感高度地道吻合。

请严格输出合法的 JSON 格式，不要输出任何额外的解释或开场白，格式如下：
{
  "domain": "专业领域（例如：NBA篮球论坛、Quora/计算机科学问答、Quora/职场晋升故事、YouTube战术分析等）",
  "topic": "核心讨论主题（一句话概括，例如：探讨为什么美国科技公司偏好扁平化管理）",
  "style": "行文或表达风格（例如：第一人称职场口吻、专业技术论文、Reddit调侃黑话、真诚叙事）",
  "summary": "全局背景简要说明（供段落翻译时参考背景与人设口吻，50-100字）",
  "extracted_terms": [
    {
      "source": "英文术语、黑话或行业缩写",
      "target": "符合该语境的准确中文地道翻译（严禁直译误翻）",
      "note": "简短说明为什么这么翻"
    }
  ]
}
"""

async def analyze_web_or_video(url: str, title: str, site_type: str, content: str) -> Dict:
    """
    调用本地 hy7b 模型，分析网页或 YouTube 视频的全局上下文与专业术语
    """
    # 截取前 2500 字符，避免超出单次上下文
    truncated_content = content[:2500] if content else ""
    
    user_prompt = f"""请分析以下【{site_type.upper()}】内容：
- URL: {url}
- 标题: {title}
- 页面摘要/正文内容:
\"\"\"
{truncated_content}
\"\"\"

请提取出该页面的领域、讨论主题、风格背景，并提取出容易被机器直译误翻的专有术语（特别是 NBA/篮球/社区黑话等）。严格输出 JSON。"""

    headers = {
        "Authorization": f"Bearer {settings.UPSTREAM_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": settings.DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2
    }
    
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{settings.UPSTREAM_BASE_URL}/chat/completions",
                headers=headers,
                json=payload
            )
            resp.raise_for_status()
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"].strip()
            
            # 解析 JSON（去除可能附带的 markdown 格式代码块）
            cleaned_text = re.sub(r'^```json\s*', '', raw_text)
            cleaned_text = re.sub(r'\s*```$', '', cleaned_text).strip()
            
            # 尝试提取首个匹配的 {...}
            match = re.search(r'\{.*\}', cleaned_text, re.DOTALL)
            if match:
                result = json.loads(match.group(0))
            else:
                result = json.loads(cleaned_text)
                
    except Exception as e:
        # 降级兜底方案
        print(f"[Analyzer Error] {e}")
        # 根据 URL 或标题进行基础推测
        is_nba = any(k in (url + title).lower() for k in ["nba", "basketball", "lakers", "warriors", "celtics", "lebron", "curry"])
        result = {
            "domain": "NBA篮球/社区讨论" if is_nba else "通用网页",
            "topic": title,
            "style": "口语化/社区讨论" if ("reddit" in url or "youtube" in url) else "标准",
            "summary": f"关于 {title} 的讨论",
            "extracted_terms": []
        }

    # 1. 保存当前活跃上下文到数据库
    save_context(
        url=url,
        title=title,
        site_type=site_type,
        domain=result.get("domain", ""),
        topic=result.get("topic", ""),
        style=result.get("style", ""),
        summary=result.get("summary", "")
    )

    # 2. 自动沉淀提取到的术语到术语库（经过严格校验）
    extracted_terms = result.get("extracted_terms", [])
    if isinstance(extracted_terms, list):
        for term in extracted_terms:
            src = term.get("source", "").strip()
            tgt = term.get("target", "").strip()
            note = term.get("note", "").strip()
            cat = result.get("domain", "提取/未分类")
            if is_valid_term(src, tgt):
                add_or_update_term(source=src, target=tgt, category=cat, note=note, source_url=url)

    return result

async def extract_terms_from_translation(source_text: str, target_text: str, domain: str = "") -> List[Dict]:
    """
    在翻译完成后的轻量级后台异步术语提取
    """
    prompt = f"""从以下英汉翻译对照中，识别出可能具有专业性、俚语色彩或容易直译错误的术语（如无明显专有术语，请返回空数组 []）：
英文: {source_text[:1000]}
中文: {target_text[:1000]}

输出格式必须为 JSON 数组：
[
  {{"source": "英文词", "target": "对应中文", "note": "解释"}}
]"""
    headers = {
        "Authorization": f"Bearer {settings.UPSTREAM_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": settings.DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": "你是一个术语提取器，严格输出纯 JSON 数组。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.UPSTREAM_BASE_URL}/chat/completions",
                headers=headers,
                json=payload
            )
            if resp.status_code == 200:
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                cleaned = re.sub(r'^```json\s*', '', raw)
                cleaned = re.sub(r'\s*```$', '', cleaned).strip()
                match = re.search(r'\[.*\]', cleaned, re.DOTALL)
                if match:
                    items = json.loads(match.group(0))
                    for item in items:
                        src = item.get("source", "").strip()
                        tgt = item.get("target", "").strip()
                        note = item.get("note", "").strip()
                        if is_valid_term(src, tgt):
                            add_or_update_term(source=src, target=tgt, category=domain or "自动提取", note=note)
                    return items
    except Exception as e:
        pass
    return []

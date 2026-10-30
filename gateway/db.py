import os
import sqlite3
import re
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from gateway.config import settings
from gateway.presets import BASKETBALL_PRESETS

def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(settings.DATABASE_PATH), exist_ok=True)
    conn = sqlite3.connect(settings.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()
    
    # 术语表
    c.execute("""
    CREATE TABLE IF NOT EXISTS terms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL UNIQUE,
        target TEXT NOT NULL,
        category TEXT DEFAULT '通用',
        note TEXT DEFAULT '',
        source_url TEXT DEFAULT '',
        hit_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 网页与视频上下文记录表
    c.execute("""
    CREATE TABLE IF NOT EXISTS contexts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        site_type TEXT DEFAULT 'web',
        domain TEXT DEFAULT '',
        topic TEXT DEFAULT '',
        style TEXT DEFAULT '',
        summary TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 活跃上下文状态表（记录当前浏览器正在浏览哪个页面/视频）
    c.execute("""
    CREATE TABLE IF NOT EXISTS active_context (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        url TEXT,
        title TEXT,
        site_type TEXT,
        domain TEXT,
        topic TEXT,
        style TEXT,
        summary TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 翻译结果本地高频缓存表（哈希键去重，0ms响应，减轻本地 7B 模型负担）
    c.execute("""
    CREATE TABLE IF NOT EXISTS translation_cache (
        cache_key TEXT PRIMARY KEY,
        source_text TEXT NOT NULL,
        target_text TEXT NOT NULL,
        model TEXT NOT NULL,
        hit_count INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 检查并预热高质量预置术语
    for item in BASKETBALL_PRESETS:
        c.execute("""
        INSERT OR IGNORE INTO terms (source, target, category, note)
        VALUES (?, ?, ?, ?)
        """, (item["source"], item["target"], item.get("category", "篮球"), item.get("note", "")))

    conn.commit()
    conn.close()

# ----------------- 术语操作 -----------------

def get_all_terms(search: Optional[str] = None, category: Optional[str] = None, limit: int = 200, offset: int = 0) -> Tuple[List[Dict], int]:
    conn = get_connection()
    c = conn.cursor()
    
    query = "SELECT * FROM terms WHERE 1=1"
    params = []
    
    if search:
        query += " AND (source LIKE ? OR target LIKE ? OR note LIKE ?)"
        s = f"%{search}%"
        params.extend([s, s, s])
        
    if category and category != "全部":
        query += " AND category = ?"
        params.append(category)
        
    count_query = f"SELECT COUNT(*) FROM ({query})"
    c.execute(count_query, params)
    total = c.fetchone()[0]
    
    query += " ORDER BY hit_count DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    c.execute(query, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows, total

def get_term_categories() -> List[str]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT DISTINCT category FROM terms ORDER BY category")
    cats = [r[0] for r in c.fetchall() if r[0]]
    conn.close()
    return cats

def get_term_by_id(term_id: int) -> Optional[Dict]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM terms WHERE id = ?", (term_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def update_term_by_id(term_id: int, source: str, target: str, category: str, note: str) -> Optional[Dict]:
    conn = get_connection()
    c = conn.cursor()
    source_clean = source.strip()
    target_clean = target.strip()
    c.execute("""
    UPDATE terms
    SET source = ?, target = ?, category = ?, note = ?, updated_at = CURRENT_TIMESTAMP
    WHERE id = ?
    """, (source_clean, target_clean, category.strip(), note.strip(), term_id))
    affected = c.rowcount > 0
    conn.commit()
    row = None
    if affected:
        c.execute("SELECT * FROM terms WHERE id = ?", (term_id,))
        row = dict(c.fetchone())
        # 清理该词相关的旧翻译缓存，确保新译法立即生效
        c.execute("DELETE FROM translation_cache WHERE source_text LIKE ?", (f"%{source_clean}%",))
        conn.commit()
    conn.close()
    return row

def add_or_update_term(source: str, target: str, category: str = "篮球/术语", note: str = "", source_url: str = "") -> Dict:
    conn = get_connection()
    c = conn.cursor()
    source_clean = source.strip()
    target_clean = target.strip()
    
    # 过滤无效占位符或自映射
    if not is_valid_term(source_clean, target_clean):
        conn.close()
        return {}
        
    # 检查是否已存在同名词条（大小写不敏感匹配，防止 yaxel 与 Yaxel 产生冲突双胞胎）
    c.execute("SELECT id FROM terms WHERE LOWER(source) = LOWER(?)", (source_clean,))
    existing = c.fetchone()
    if existing:
        term_id = existing[0]
        c.execute("""
        UPDATE terms
        SET source = ?, target = ?,
            category = CASE WHEN ? != '' THEN ? ELSE category END,
            note = CASE WHEN ? != '' THEN ? ELSE note END,
            source_url = CASE WHEN ? != '' THEN ? ELSE source_url END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """, (source_clean, target_clean, category, category, note, note, source_url, source_url, term_id))
    else:
        c.execute("""
        INSERT INTO terms (source, target, category, note, source_url, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (source_clean, target_clean, category, note, source_url))
    
    conn.commit()
    c.execute("SELECT * FROM terms WHERE LOWER(source) = LOWER(?)", (source_clean,))
    row = dict(c.fetchone())
    
    # 自动清理受该词影响的旧翻译缓存，防止旧错误翻译因缓存继续呈现
    c.execute("DELETE FROM translation_cache WHERE source_text LIKE ?", (f"%{source_clean}%",))
    conn.commit()
    conn.close()
    return row

def delete_term(term_id: int) -> bool:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT source FROM terms WHERE id = ?", (term_id,))
    row = c.fetchone()
    source_to_clear = row[0] if row else ""
    
    c.execute("DELETE FROM terms WHERE id = ?", (term_id,))
    affected = c.rowcount > 0
    if affected and source_to_clear:
        c.execute("DELETE FROM translation_cache WHERE source_text LIKE ?", (f"%{source_to_clear}%",))
    conn.commit()
    conn.close()
    return affected

def is_valid_term(source: str, target: str) -> bool:
    """
    严格过滤无效术语、占位符或整句段落，防止污染词库：
    1. 过滤沉浸式翻译段落标签（如 p0, p1, [[p0]], <p id='0'>, 1, 2 等）
    2. 过滤长度异常的句子（术语通常为 1-4 个单词，长度不超过 40 字符）
    3. 过滤中文整句作为 target（target 通常为简短词组，不超过 30 字符）
    """
    src = source.strip()
    tgt = target.strip()
    if not src or not tgt:
        return False
    # 过滤纯数字或形如 p0, p1, p99, [p0], [[p0]]
    if re.fullmatch(r'\[*p?\d+\]*', src, re.IGNORECASE):
        return False
    # 过滤单字符或过长短语
    if len(src) < 2 or len(src) > 40:
        return False
    # 单词数过滤（术语一般不超过 4 个词）
    if len(src.split()) > 4:
        return False
    # 目标中文过长（说明提取成了整句话甚至段落）
    if len(tgt) > 30:
        return False
    # 忽略原词与目标词完全相同且无中文的无意义条目（如 yaxel -> yaxel）
    if src.lower() == tgt.lower():
        return False
    return True

def build_fuzzy_term_regex(term_source: str) -> re.Pattern:
    """
    为英文术语生成智能词形还原正则，支持:
    - 词尾时态: brick -> brick, bricked, bricking, bricks
    - 复数形式: franchise -> franchise, franchises; carry -> carry, carries, carried, carrying
    - 连字符/空格互通: pick and roll <-> pick-and-roll; step-back <-> step back
    """
    clean = term_source.strip()
    # 将连字符或空格替换为支持任意空格或连字符的模式
    tokens = re.split(r'[\s\-]+', clean)
    regex_parts = []
    
    for idx, token in enumerate(tokens):
        escaped = re.escape(token)
        # 仅对单末尾词或非缩写字母做轻量屈折变化扩展 (>=3字母且不全是大写)
        if len(token) >= 3 and not token.isupper():
            if token.endswith('y') and len(token) > 3 and token[-2] not in 'aeiou':
                # carry -> carri(ed|es)|carry(ing)?
                stem = re.escape(token[:-1])
                var = f"(?:{stem}(?:ies|ied)|{escaped}(?:ing)?)"
            elif token.endswith('e'):
                # posterize -> posteriz(e|ed|ing|es)
                stem = re.escape(token[:-1])
                var = f"{stem}(?:e|ed|ing|es)?"
            else:
                # brick -> brick(s|ed|ing)?
                var = f"{escaped}(?:s|es|ed|ing)?"
            regex_parts.append(var)
        else:
            regex_parts.append(escaped)
            
    pattern_str = r'\b' + r'[\s\-]+'.join(regex_parts) + r'\b'
    return re.compile(pattern_str, re.IGNORECASE)

def find_matching_terms(text: str, limit: int = 50) -> List[Dict]:
    """
    根据待翻译的文本，精准匹配术语库中的词汇（支持单复数、时态、连字符等屈折变化）。
    同时增加命中计数。
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM terms")
    all_terms = [dict(r) for r in c.fetchall()]
    
    matched = []
    matched_ids = []
    seen_sources = set()
    
    # 按照原词长度从长到短排序，优先匹配复合专有名词
    all_terms.sort(key=lambda x: len(x["source"]), reverse=True)
    
    for t in all_terms:
        # 跳过非法占位符或无效自映射
        if not is_valid_term(t["source"], t["target"]):
            continue
            
        src_lower = t["source"].strip().lower()
        if src_lower in seen_sources:
            continue
            
        # 处理带有斜杠的复合词条（如 "cook / cooked"）
        sub_sources = [s.strip() for s in t["source"].split('/') if s.strip()]
        is_hit = False
        for s in sub_sources:
            try:
                reg = build_fuzzy_term_regex(s)
                if reg.search(text):
                    is_hit = True
                    break
            except Exception:
                if re.search(r'\b' + re.escape(s.lower()) + r'\b', text.lower()):
                    is_hit = True
                    break
                    
        if is_hit:
            matched.append(t)
            matched_ids.append(t["id"])
            seen_sources.add(src_lower)
            if len(matched) >= limit:
                break
                
    if matched_ids:
        # 更新命中统计
        placeholders = ",".join("?" * len(matched_ids))
        c.execute(f"UPDATE terms SET hit_count = hit_count + 1 WHERE id IN ({placeholders})", matched_ids)
        conn.commit()
        
    conn.close()
    return matched

# ----------------- 上下文操作 -----------------

def save_context(url: str, title: str, site_type: str, domain: str, topic: str, style: str, summary: str):
    conn = get_connection()
    c = conn.cursor()
    
    # 写入历史上下文表
    c.execute("""
    INSERT INTO contexts (url, title, site_type, domain, topic, style, summary, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(url) DO UPDATE SET
        title = excluded.title,
        site_type = excluded.site_type,
        domain = excluded.domain,
        topic = excluded.topic,
        style = excluded.style,
        summary = excluded.summary,
        updated_at = CURRENT_TIMESTAMP
    """, (url, title, site_type, domain, topic, style, summary))
    
    # 更新当前活跃状态
    c.execute("""
    INSERT INTO active_context (id, url, title, site_type, domain, topic, style, summary, updated_at)
    VALUES (1, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(id) DO UPDATE SET
        url = excluded.url,
        title = excluded.title,
        site_type = excluded.site_type,
        domain = excluded.domain,
        topic = excluded.topic,
        style = excluded.style,
        summary = excluded.summary,
        updated_at = CURRENT_TIMESTAMP
    """, (url, title, site_type, domain, topic, style, summary))
    
    conn.commit()
    conn.close()

def get_active_context() -> Optional[Dict]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM active_context WHERE id = 1")
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

# ----------------- 缓存操作 -----------------

def get_cached_translation(cache_key: str) -> Optional[str]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT target_text FROM translation_cache WHERE cache_key = ?", (cache_key,))
    row = c.fetchone()
    if row:
        c.execute("UPDATE translation_cache SET hit_count = hit_count + 1, updated_at = CURRENT_TIMESTAMP WHERE cache_key = ?", (cache_key,))
        conn.commit()
    conn.close()
    return row[0] if row else None

def set_cached_translation(cache_key: str, source_text: str, target_text: str, model: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
    INSERT INTO translation_cache (cache_key, source_text, target_text, model, updated_at)
    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(cache_key) DO UPDATE SET
        target_text = excluded.target_text,
        hit_count = translation_cache.hit_count + 1,
        updated_at = CURRENT_TIMESTAMP
    """, (cache_key, source_text, target_text, model))
    conn.commit()
    conn.close()
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM active_context WHERE id = 1")
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

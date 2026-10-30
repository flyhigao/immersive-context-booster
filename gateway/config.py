import os
from pydantic import BaseModel

def load_env_file():
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v

load_env_file()

class Settings(BaseModel):
    # 网关监听配置
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8787"))

    # 反代路径前缀（支持留空或指定随机安全目录，如 custom_prefix）
    raw_prefix: str = os.getenv("PATH_PREFIX", "").strip().strip("/")
    PATH_PREFIX: str = f"/{raw_prefix}" if raw_prefix else ""

    # 上游 LLM 配置（从 .env 或环境变量中读取，避免源码硬编码泄露）
    UPSTREAM_BASE_URL: str = os.getenv("UPSTREAM_BASE_URL", "http://127.0.0.1:8000/v1")
    UPSTREAM_API_KEY: str = os.getenv("UPSTREAM_API_KEY", "")
    DEFAULT_MODEL: str = os.getenv("DEFAULT_MODEL", "hy7b")

    # 数据存储
    DATABASE_PATH: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "terms.db")

    # 并发限制（保护 7B 显卡不被沉浸式翻译突发几十个请求打爆，默认并发限制为 3）
    MAX_CONCURRENCY: int = int(os.getenv("MAX_CONCURRENCY", "3"))

    # 翻译结果缓存开关
    ENABLE_CACHE: bool = os.getenv("ENABLE_CACHE", "true").lower() in ("true", "1", "yes")

    # 备用云端模型配置（可选，双模型路由平滑降级）
    BACKUP_BASE_URL: str = os.getenv("BACKUP_BASE_URL", "")
    BACKUP_API_KEY: str = os.getenv("BACKUP_API_KEY", "")
    BACKUP_MODEL: str = os.getenv("BACKUP_MODEL", "")

    # 4. 自动提取术语开关（默认关闭自动异步提取，由用户通过前端/划词精确添加，彻底避免 AI 幻觉污染）
    AUTO_EXTRACT_TERMS: bool = os.getenv("AUTO_EXTRACT_TERMS", "false").lower() in ("true", "1", "yes")

    # 5. 英文人名/专有昵称保留规则（默认开启：所有英文人名、球员姓名严格保留英文原词，禁止音译成中文或扩写全名）
    KEEP_NAMES_UNTRANSLATED: bool = os.getenv("KEEP_NAMES_UNTRANSLATED", "true").lower() in ("true", "1", "yes")

settings = Settings()

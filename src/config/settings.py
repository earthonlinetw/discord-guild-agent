"""Discord AI Agent 系統設定定義。

使用 Pydantic v2 定義所有設定結構，支援 YAML 載入與環境變數替換。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


# ============================================================
# 子設定模型
# ============================================================


class AIConfig(BaseModel):
    """AI Provider 設定。"""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout: int = 60
    # 視覺模型（用於解析圖片）。留空則沿用 model。需支援多模態（vision）。
    vision_model: str = ""
    # 是否啟用圖片分析（vision）功能
    vision_enabled: bool = True


class MessageConfig(BaseModel):
    """訊息收集設定。"""

    batch_size: int = 5
    mention_priority: bool = True
    other_channel_trigger_count: int = 20
    other_channel_trigger_cooldown_seconds: int = 30
    max_other_channel_summaries: int = 12


class ContextConfig(BaseModel):
    """Context / Token Budget 設定。"""

    limit_tokens: int = 20000
    summary_threshold: float = 0.7


class QueueConfig(BaseModel):
    """任務佇列設定。"""

    max_concurrent: int = 3
    retry_max: int = 3
    retry_delay: int = 30


class CouncilConfig(BaseModel):
    """AI Council 設定。"""

    enabled: bool = True
    channel_name: str = "ai-council"
    max_messages: int = 10


class GuildEventConfig(BaseModel):
    """Guild 事件處理設定。"""

    enabled: bool = True
    agent_name: str = "Bob"
    log_channel_name: str = "moderator-only"
    debounce_seconds: int = 2
    max_batch_size: int = 25
    include_events: list[str] = Field(
        default_factory=lambda: [
            "guild_update",
            "role_create",
            "role_update",
            "role_delete",
            "channel_create",
            "channel_update",
            "channel_delete",
        ]
    )


class DashboardConfig(BaseModel):
    """Dashboard Web 面板設定。"""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    title: str = "Discord Guild Agent Dashboard"


class OverrideConfig(BaseModel):
    """Human Override 設定。"""

    enabled: bool = True
    approve_command: str = "!approve"
    deny_command: str = "!deny"
    stop_command: str = "!stop"


class DatabaseConfig(BaseModel):
    """資料庫設定。"""

    url: str = "sqlite+aiosqlite:///data/agent.db"
    pool_size: int = 5


class LoggingConfig(BaseModel):
    """日誌設定。"""

    level: str = "INFO"
    format: str = "console"  # console | json


class AgentConfig(BaseModel):
    """單一 Agent 人格設定。"""

    name: str
    token: str
    personality: str = ""
    system_prompt: str = ""
    enabled: bool = True  # 是否啟用此 Agent（設 false 則不啟動該 bot）


# ============================================================
# 頂層設定模型
# ============================================================


class AppConfig(BaseModel):
    """應用程式完整設定。"""

    ai: AIConfig = Field(default_factory=AIConfig)
    message: MessageConfig = Field(default_factory=MessageConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    council: CouncilConfig = Field(default_factory=CouncilConfig)
    guild_events: GuildEventConfig = Field(default_factory=GuildEventConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    override: OverrideConfig = Field(default_factory=OverrideConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    agents: list[AgentConfig] = Field(default_factory=list)


@dataclass
class BufferedGuildEvent:
    """短時間內待合併的 guild event。"""

    event_type: str
    description: str


@dataclass
class PendingGuildEventBatch:
    """單一 guild / agent 的暫存 guild event 批次。"""

    guild_id: str
    agent_name: str
    log_channel_id: str
    events: list[BufferedGuildEvent] = field(default_factory=list)
    flush_task: Any = None


# ============================================================
# 設定載入器
# ============================================================

# 環境變數佔位符模式，例如 ${VAR_NAME} 或 ${VAR_NAME:-default}
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_env(value: str) -> str:
    """遞迴解析字串中的 ${ENV_VAR} 佔位符。"""

    def _replacer(match: re.Match[str]) -> str:
        expr = match.group(1)
        # 支援 ${VAR:-default} 語法
        if ":-" in expr:
            var_name, default = expr.split(":-", 1)
            return os.environ.get(var_name.strip(), default.strip())
        return os.environ.get(expr.strip(), match.group(0))

    # 反覆解析直到沒有更多佔位符
    previous = None
    while previous != value:
        previous = value
        value = _ENV_PATTERN.sub(_replacer, value)
    return value


def _walk_resolve(obj: Any) -> Any:
    """深度遍歷資料結構，解析所有字串中的環境變數。"""
    if isinstance(obj, str):
        return _resolve_env(obj)
    if isinstance(obj, dict):
        return {k: _walk_resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_resolve(item) for item in obj]
    return obj


class ConfigLoader:
    """設定檔載入器，從 YAML + 環境變數合併設定。"""

    @staticmethod
    def load(path: str | Path) -> AppConfig:
        """從 YAML 檔案載入設定，自動替換環境變數。

        Args:
            path: YAML 設定檔路徑。

        Returns:
            完整的 AppConfig 實例。

        Raises:
            FileNotFoundError: 設定檔不存在。
            yaml.YAMLError: YAML 解析錯誤。
        """
        config_path = Path(path)

        # 載入 .env 檔案（與設定檔同目錄或專案根目錄）
        env_path = config_path.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        else:
            # 嘗試目前工作目錄的 .env
            load_dotenv()

        if not config_path.exists():
            raise FileNotFoundError(f"設定檔不存在: {config_path}")

        with open(config_path, "r", encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        # 解析環境變數佔位符
        resolved = _walk_resolve(raw)

        return AppConfig.model_validate(resolved)

    @staticmethod
    def load_from_dict(data: dict[str, Any]) -> AppConfig:
        """從字典建立設定（用於測試）。"""
        resolved = _walk_resolve(data)
        return AppConfig.model_validate(resolved)

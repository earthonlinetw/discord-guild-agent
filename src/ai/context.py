"""Context 管理器 — Token Budget System。

管理對話歷史的 Token 預算，超出限制時自動摘要舊訊息。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.config.settings import ContextConfig

logger = structlog.get_logger(__name__)


# ============================================================
# Token 估算
# ============================================================

# 粗略估算：1 個中文字 ≈ 2 token，1 個英文單字 ≈ 1.3 token
# 這是簡化版本，正式環境建議使用 tiktoken
_AVERAGE_CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    """估算文字的 Token 數量。

    使用字元數除以平均比率來粗估。
    生產環境建議改用 tiktoken 精確計算。

    Args:
        text: 輸入文字。

    Returns:
        估算的 Token 數量。
    """
    if not text:
        return 0
    return max(1, len(text) // _AVERAGE_CHARS_PER_TOKEN)


# ============================================================
# Context 管理器
# ============================================================


@dataclass
class MessageBlock:
    """訊息區塊，用於組織對話歷史。"""

    role: str  # system / user / assistant / tool
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    estimated_tokens: int = 0


class ContextManager:
    """Token Budget 管理器。

    負責組織送給 AI 的對話歷史，在 Token 預算內保留最有價值的上下文。
    當總 Token 數接近限制時，觸發摘要機制壓縮舊訊息。
    """

    def __init__(self, config: ContextConfig) -> None:
        """初始化。

        Args:
            config: Context 設定（limit_tokens / summary_threshold）。
        """
        self._config = config
        self._blocks: list[MessageBlock] = []

    @property
    def token_budget(self) -> int:
        """Token 預算上限。"""
        return self._config.limit_tokens

    @property
    def summary_threshold(self) -> float:
        """觸發摘要的門檻比例。"""
        return self._config.summary_threshold

    def current_tokens(self) -> int:
        """計算目前總 Token 數。"""
        return sum(b.estimated_tokens for b in self._blocks)

    def needs_summary(self) -> bool:
        """判斷是否需要摘要壓縮。"""
        return self.current_tokens() >= int(self.token_budget * self.summary_threshold)

    # ---- 訊息管理 ----

    def add_system(self, content: str) -> None:
        """新增 system 訊息。"""
        tokens = estimate_tokens(content)
        self._blocks.append(MessageBlock(
            role="system", content=content, estimated_tokens=tokens
        ))

    def add_user(self, content: str, name: str | None = None) -> None:
        """新增 user 訊息。"""
        tokens = estimate_tokens(content)
        self._blocks.append(MessageBlock(
            role="user", content=content, name=name, estimated_tokens=tokens
        ))

    def add_assistant(
        self,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """新增 assistant 訊息。"""
        tokens = estimate_tokens(content)
        self._blocks.append(MessageBlock(
            role="assistant", content=content, tool_calls=tool_calls, estimated_tokens=tokens
        ))

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """新增 tool 執行結果訊息。"""
        tokens = estimate_tokens(content)
        self._blocks.append(MessageBlock(
            role="tool", content=content, tool_call_id=tool_call_id, estimated_tokens=tokens
        ))

    # ---- 歷史壓縮 ----

    def compress_with_summary(self, summary: str) -> None:
        """用摘要替換舊訊息，釋放 Token 空間。

        保留 system 訊息 + 摘要 + 最近 N 條訊息。

        Args:
            summary: 由 Summary Service 產生的摘要文字。
        """
        # 分離 system 訊息與其他訊息
        system_blocks = [b for b in self._blocks if b.role == "system"]
        other_blocks = [b for b in self._blocks if b.role != "system"]

        # 保留最近 5 條非 system 訊息
        recent_count = min(5, len(other_blocks))
        recent_blocks = other_blocks[-recent_count:] if recent_count > 0 else []

        # 建立摘要 block
        summary_tokens = estimate_tokens(summary)
        summary_block = MessageBlock(
            role="system",
            content=f"[歷史對話摘要]\n{summary}",
            estimated_tokens=summary_tokens,
        )

        # 重組
        self._blocks = system_blocks + [summary_block] + recent_blocks
        logger.info(
            "context.compressed",
            original_tokens=sum(b.estimated_tokens for b in other_blocks),
            new_tokens=self.current_tokens(),
        )

    # ---- 輸出格式化 ----

    def to_messages(self) -> list[dict[str, Any]]:
        """將內部訊息轉換為 OpenAI API 的 messages 格式。

        Returns:
            OpenAI messages list。
        """
        messages: list[dict[str, Any]] = []
        for block in self._blocks:
            msg: dict[str, Any] = {"role": block.role, "content": block.content}
            if block.name:
                msg["name"] = block.name
            if block.tool_call_id:
                msg["tool_call_id"] = block.tool_call_id
            if block.tool_calls:
                msg["tool_calls"] = block.tool_calls
            messages.append(msg)
        return messages

    def clear(self) -> None:
        """清除所有訊息。"""
        self._blocks.clear()

    def rebuild_from_db(
        self,
        recent_messages: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        long_term_memory: list[dict[str, Any]],
        system_prompt: str,
        agent_name: str = "",
    ) -> None:
        """從資料庫重建完整 Context。

        Args:
            recent_messages: 最近的訊息紀錄。
            summaries: 歷史摘要。
            long_term_memory: 長期記憶。
            system_prompt: Agent 的 system prompt。
            agent_name: 目前 Agent 名稱，用於辨識哪些 bot 訊息才是自己的 assistant 歷史。
        """
        self.clear()

        # 1. System prompt
        self.add_system(system_prompt)

        # 2. 長期記憶注入
        if long_term_memory:
            memory_text = "\n".join(
                f"- [{m['category']}] {m['key']}: {m['value']}"
                for m in long_term_memory
            )
            self.add_system(f"[伺服器長期記憶]\n{memory_text}")

        # 3. 歷史摘要
        if summaries:
            summary_text = "\n\n".join(
                f"[{s['start_time']} ~ {s['end_time']}]\n{s['summary']}"
                for s in summaries
            )
            self.add_system(f"[歷史對話摘要]\n{summary_text}")

        # 4. 最近訊息
        for msg in reversed(recent_messages):  # DB 回傳 DESC，需反轉
            author_name = msg.get("author_name", "Unknown")
            content = msg.get("content", "")
            if msg.get("is_bot"):
                if agent_name and author_name == agent_name:
                    self.add_assistant(content)
                else:
                    self.add_user(f"[{author_name} (其他 bot)]: {content}")
            else:
                self.add_user(f"{author_name}: {content}")

        logger.info(
            "context.rebuilt",
            messages=len(recent_messages),
            summaries=len(summaries),
            memory_items=len(long_term_memory),
            total_tokens=self.current_tokens(),
        )

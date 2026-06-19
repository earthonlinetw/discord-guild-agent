"""Tool 基礎類別與資料結構。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Coroutine, Callable

import structlog

logger = structlog.get_logger(__name__)


# ============================================================
# 安全等級
# ============================================================


class SafetyLevel(str, Enum):
    """Tool 安全等級。"""

    SAFE = "SAFE"
    MODERATE = "MODERATE"
    DANGEROUS = "DANGEROUS"


# ============================================================
# Tool 執行結果
# ============================================================


@dataclass
class ToolResult:
    """Tool 執行結果。"""

    success: bool = True
    data: Any = None
    error: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """轉換為字典（用於回傳給 AI）。"""
        result: dict[str, Any] = {"success": self.success}
        if self.data is not None:
            result["data"] = self.data
        if self.error:
            result["error"] = self.error
        if self.message:
            result["message"] = self.message
        return result


# ============================================================
# Tool 基礎類別
# ============================================================


class ToolBase:
    """Tool 基礎類別。

    所有 Discord 管理工具皆繼承此類別。
    定義統一的介面與安全等級標註。
    """

    # 子類別必須覆寫這些屬性
    name: str = ""
    description: str = ""
    safety_level: SafetyLevel = SafetyLevel.SAFE
    parameters_schema: dict[str, Any] = {}

    async def execute(self, **kwargs: Any) -> ToolResult:
        """執行工具。

        Args:
            **kwargs: 工具參數。

        Returns:
            執行結果。
        """
        raise NotImplementedError(f"Tool {self.name} 未實作 execute()")

    def to_openai_schema(self) -> dict[str, Any]:
        """轉換為 OpenAI Function Calling schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"[{self.safety_level.value}] {self.description}",
                "parameters": self.parameters_schema,
            },
        }

    def __repr__(self) -> str:
        return f"<Tool:{self.name} safety={self.safety_level.value}>"

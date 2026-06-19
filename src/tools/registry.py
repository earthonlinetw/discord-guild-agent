"""Tool 註冊中心。

管理所有可用的 Tool，提供查詢、安全等級檢查。
"""

from __future__ import annotations

from typing import Any

import structlog

from src.tools.base import ToolBase, SafetyLevel, ToolResult

logger = structlog.get_logger(__name__)


class ToolRegistry:
    """Tool 註冊中心。

    集中管理所有 Tool 的註冊、查詢、執行。
    支援依安全等級篩選。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolBase] = {}

    def register(self, tool: ToolBase) -> None:
        """註冊一個 Tool。"""
        if tool.name in self._tools:
            logger.warning("tool.duplicate", name=tool.name)
        self._tools[tool.name] = tool
        logger.debug("tool.registered", name=tool.name, safety=tool.safety_level.value)

    def register_many(self, tools: list[ToolBase]) -> None:
        """批次註冊 Tool。"""
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> ToolBase | None:
        """取得指定 Tool。"""
        return self._tools.get(name)

    def get_all(self) -> list[ToolBase]:
        """取得所有已註冊的 Tool。"""
        return list(self._tools.values())

    def get_by_safety(self, level: SafetyLevel) -> list[ToolBase]:
        """依安全等級篩選 Tool。"""
        return [t for t in self._tools.values() if t.safety_level == level]

    def get_openai_schemas(self) -> list[dict[str, Any]]:
        """取得所有 Tool 的 OpenAI Function Calling schema。"""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def get_safety_map(self) -> dict[str, str]:
        """取得 Tool 名稱 → 安全等級映射。"""
        return {name: tool.safety_level.value for name, tool in self._tools.items()}

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> ToolResult:
        """執行指定 Tool。

        Args:
            tool_name: Tool 名稱。
            **kwargs: Tool 參數。

        Returns:
            執行結果。

        Raises:
            KeyError: Tool 不存在。
        """
        tool = self._tools.get(tool_name)
        if not tool:
            logger.error("tool.not_found", name=tool_name)
            return ToolResult(success=False, error=f"Tool '{tool_name}' 不存在")

        logger.info(
            "tool.executing",
            name=tool_name,
            safety=tool.safety_level.value,
            params=list(kwargs.keys()),
        )

        try:
            result = await tool.execute(**kwargs)
            logger.info(
                "tool.executed",
                name=tool_name,
                success=result.success,
            )
            return result
        except Exception as exc:
            logger.error("tool.execution_error", name=tool_name, error=str(exc))
            return ToolResult(success=False, error=str(exc))

    @property
    def tool_count(self) -> int:
        """已註冊 Tool 數量。"""
        return len(self._tools)

    @property
    def tool_names(self) -> list[str]:
        """所有 Tool 名稱。"""
        return list(self._tools.keys())

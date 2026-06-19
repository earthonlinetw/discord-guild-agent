"""OpenAI Compatible AI Provider。

支援 OpenAI、OpenRouter、Ollama、LM Studio、vLLM 等相容 API。
透過 base_url / api_key / model 三個參數即可切換 Provider。
實作 Tool Calling（Function Calling）機制。
"""

from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError

import structlog

from src.config.settings import AIConfig

logger = structlog.get_logger(__name__)


# ============================================================
# 資料結構
# ============================================================


@dataclass
class ToolDefinition:
    """Tool 定義，對應 OpenAI function calling schema。"""

    name: str
    description: str
    parameters: dict[str, Any]
    safety_level: str = "SAFE"  # SAFE / MODERATE / DANGEROUS


@dataclass
class ReasoningOutput:
    """AI 推理輸出（執行工具前的 reasoning）。"""

    reason: str
    action: str
    expected_result: str


@dataclass
class AIResponse:
    """AI 回應結果。"""

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: ReasoningOutput | None = None
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""


# ============================================================
# AI Provider
# ============================================================


class AIProvider:
    """OpenAI Compatible API 客戶端。

    支援任意相容 OpenAI API 格式的服務供應商。
    統一管理 Tool Calling 與 Reasoning 機制。
    """

    def __init__(self, config: AIConfig) -> None:
        """初始化 AI Provider。

        Args:
            config: AI 設定（base_url / api_key / model 等）。
        """
        self._config = config
        self._client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "sk-placeholder",
            timeout=config.timeout,
        )
        self._tools: list[ToolDefinition] = []
        self._tool_map: dict[str, ToolDefinition] = {}

    # ---- Tool 註冊 ----

    def register_tool(self, tool: ToolDefinition) -> None:
        """註冊一個 Tool 定義。"""
        if tool.name in self._tool_map:
            self._tools = [t for t in self._tools if t.name != tool.name]
        self._tools.append(tool)
        self._tool_map[tool.name] = tool
        logger.debug("ai.tool_registered", name=tool.name, safety=tool.safety_level)

    def register_tools(self, tools: list[ToolDefinition]) -> None:
        """批次註冊 Tool。"""
        for tool in tools:
            self.register_tool(tool)

    @property
    def tool_safety_levels(self) -> dict[str, str]:
        """取得所有 Tool 的安全等級映射。"""
        return {name: t.safety_level for name, t in self._tool_map.items()}

    # ---- 核心 AI 呼叫 ----

    def _build_tool_schemas(self) -> list[dict[str, Any]]:
        """將 ToolDefinition 轉換為 OpenAI function schema。"""
        schemas: list[dict[str, Any]] = []
        for tool in self._tools:
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            })
        return schemas

    def _parse_reasoning(self, content: str) -> ReasoningOutput | None:
        """嘗試從 AI 輸出中解析 reasoning JSON。"""
        if not content:
            return None
        # 嘗試找出 JSON 區塊
        try:
            # 優先嘗試整段解析
            data = json.loads(content.strip())
            if isinstance(data, dict) and "reason" in data and "action" in data:
                return ReasoningOutput(
                    reason=data.get("reason", ""),
                    action=data.get("action", ""),
                    expected_result=data.get("expected_result", ""),
                )
        except json.JSONDecodeError:
            pass

        # 嘗試從 markdown code block 中提取
        import re
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1).strip())
                if isinstance(data, dict) and "reason" in data:
                    return ReasoningOutput(
                        reason=data.get("reason", ""),
                        action=data.get("action", ""),
                        expected_result=data.get("expected_result", ""),
                    )
            except json.JSONDecodeError:
                pass
        return None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools_enabled: bool = True,
        agent_name: str = "",
    ) -> AIResponse:
        """呼叫 AI 進行對話推理。

        Args:
            messages: 對話歷史（OpenAI messages 格式）。
            tools_enabled: 是否啟用 tool calling。
            agent_name: 呼叫的 Agent 名稱（用於日誌）。

        Returns:
            AI 回應結果。
        """
        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }

        if tools_enabled and self._tools:
            kwargs["tools"] = self._build_tool_schemas()
            kwargs["tool_choice"] = "auto"

        log = logger.bind(agent=agent_name, model=self._config.model)

        try:
            log.debug("ai.request", message_count=len(messages))
            response = await self._client.chat.completions.create(**kwargs)

            choice = response.choices[0] if response.choices else None
            if not choice:
                log.warning("ai.no_choice")
                return AIResponse(finish_reason="no_choice")

            message = choice.message
            content = message.content or ""
            tool_calls_raw = []

            # 解析 tool calls
            if message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls_raw.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    })

            # 嘗試解析 reasoning
            reasoning = self._parse_reasoning(content)

            usage = {}
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            result = AIResponse(
                content=content,
                tool_calls=tool_calls_raw,
                reasoning=reasoning,
                usage=usage,
                finish_reason=choice.finish_reason or "",
            )

            log.info(
                "ai.response",
                finish_reason=choice.finish_reason,
                tool_calls=len(tool_calls_raw),
                total_tokens=usage.get("total_tokens", 0),
            )
            return result

        except APITimeoutError as exc:
            log.error("ai.timeout", error=str(exc))
            raise
        except RateLimitError as exc:
            log.error("ai.rate_limit", error=str(exc))
            raise
        except APIError as exc:
            log.error("ai.api_error", status=exc.status_code, error=str(exc))
            raise

    async def chat_with_reasoning(
        self,
        messages: list[dict[str, Any]],
        agent_name: str = "",
    ) -> AIResponse:
        """兩階段推理：先要求 AI 輸出 reasoning，再執行 tool calls。

        第一階段：system prompt 要求 AI 先輸出 reasoning JSON。
        第二階段：如果 reasoning 中指定了 action，則執行對應 tool。

        Args:
            messages: 對話歷史。
            agent_name: Agent 名稱。

        Returns:
            包含 reasoning 與 tool calls 的回應。
        """
        # 注入 reasoning 指令
        reasoning_instruction = {
            "role": "system",
            "content": (
                "在執行任何工具之前，你必須先在回應中輸出以下 JSON 格式的推理：\n"
                "```json\n"
                '{"reason": "你的推理過程", "action": "你要執行的動作", "expected_result": "預期結果"}\n'
                "```\n"
                "然後再呼叫對應的工具函數。\n"
                "如果你只需要回覆文字而不需要執行工具，則不需要輸出此 JSON。"
            ),
        }

        augmented_messages = [*messages, reasoning_instruction]
        return await self.chat(
            messages=augmented_messages,
            tools_enabled=True,
            agent_name=agent_name,
        )

    async def summarize(self, text: str, agent_name: str = "") -> str:
        """使用 AI 產生摘要。

        Args:
            text: 需要摘要的文字。
            agent_name: Agent 名稱。

        Returns:
            摘要文字。
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一個專業的摘要助手。請將以下對話內容壓縮為簡潔的摘要，"
                    "保留重要資訊、決策和結論。使用繁體中文。"
                ),
            },
            {"role": "user", "content": f"請摘要以下內容：\n\n{text}"},
        ]

        result = await self.chat(messages=messages, tools_enabled=False, agent_name=agent_name)
        return result.content

    async def analyze_image(
        self,
        image_url: str,
        prompt: str = "",
        agent_name: str = "",
    ) -> str:
        """使用視覺模型分析圖片內容。

        Args:
            image_url: 圖片的 URL（Discord 附件 URL 即可）。
            prompt: 額外的分析指示（可選）。
            agent_name: Agent 名稱（用於日誌）。

        Returns:
            圖片內容的文字描述。
        """
        if not self._config.vision_enabled:
            return ""

        vision_model = self._config.vision_model or self._config.model
        instruction = prompt or (
            "請用繁體中文詳細描述這張圖片的內容，包括主體、場景、文字（若有）、"
            "氛圍與任何值得注意的細節。若圖片含有文字，請一併轉錄。"
        )

        log = logger.bind(agent=agent_name, model=vision_model)
        try:
            response = await self._client.chat.completions.create(
                model=vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instruction},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                max_tokens=self._config.max_tokens,
                temperature=0.3,
            )
            choice = response.choices[0] if response.choices else None
            description = (choice.message.content or "") if choice else ""
            log.info("ai.vision_analyzed", chars=len(description))
            return description
        except APIError as exc:
            log.error("ai.vision_error", status=getattr(exc, "status_code", None), error=str(exc))
            return ""
        except Exception as exc:  # 模型不支援 vision 等情況
            log.error("ai.vision_unexpected", error=str(exc))
            return ""

    @property
    def model_name(self) -> str:
        """取得目前使用的模型名稱。"""
        return self._config.model

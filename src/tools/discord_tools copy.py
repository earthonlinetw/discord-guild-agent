"""Discord 管理工具集合。

所有 Discord 管理功能皆封裝為 Tool，標註安全等級。
包含：訊息管理、頻道管理、角色管理、成員管理、執行緒管理、
資訊查詢、邀請管理、審計日誌、Webhook、排程活動、表情管理。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import discord

import structlog

from src.tools.base import ToolBase, SafetyLevel, ToolResult

logger = structlog.get_logger(__name__)


# ============================================================
# 訊息管理工具
# ============================================================


class SendMessageTool(ToolBase):
    """發送訊息至指定頻道。"""

    name = "send_message"
    description = "發送訊息至指定頻道"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
            "content": {"type": "string", "description": "訊息內容"},
        },
        "required": ["channel_id", "content"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, channel_id: str, content: str, **kwargs: Any) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在或類型不符")
        msg = await channel.send(content)
        return ToolResult(success=True, data={"message_id": str(msg.id)}, message="訊息已發送")


class ReplyMessageTool(ToolBase):
    """回覆指定訊息。"""

    name = "reply_message"
    description = "回覆指定訊息"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
            "message_id": {"type": "string", "description": "訊息 ID"},
            "content": {"type": "string", "description": "回覆內容"},
        },
        "required": ["channel_id", "message_id", "content"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, channel_id: str, message_id: str, content: str, **kwargs: Any) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在")
        msg = await channel.fetch_message(int(message_id))
        reply = await msg.reply(content)
        return ToolResult(success=True, data={"message_id": str(reply.id)}, message="回覆已發送")


class ReactionMessageTool(ToolBase):
    """對訊息添加表情反應。"""

    name = "reaction_message"
    description = "對指定訊息添加表情反應"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
            "message_id": {"type": "string", "description": "訊息 ID"},
            "emoji": {"type": "string", "description": "表情符號（Unicode 或 custom emoji）"},
        },
        "required": ["channel_id", "message_id", "emoji"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, channel_id: str, message_id: str, emoji: str, **kwargs: Any) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在")
        msg = await channel.fetch_message(int(message_id))
        await msg.add_reaction(emoji)
        return ToolResult(success=True, message="表情反應已添加")


class EditMessageTool(ToolBase):
    """編輯指定訊息。"""

    name = "edit_message"
    description = "編輯指定訊息的內容"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
            "message_id": {"type": "string", "description": "訊息 ID"},
            "new_content": {"type": "string", "description": "新內容"},
        },
        "required": ["channel_id", "message_id", "new_content"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, channel_id: str, message_id: str, new_content: str, **kwargs: Any) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在")
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(content=new_content)
        return ToolResult(success=True, message="訊息已編輯")


class DeleteMessageTool(ToolBase):
    """刪除指定訊息。"""

    name = "delete_message"
    description = "刪除指定訊息"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
            "message_id": {"type": "string", "description": "訊息 ID"},
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, channel_id: str, message_id: str, reason: str = "", **kwargs: Any) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在")
        msg = await channel.fetch_message(int(message_id))
        await msg.delete(reason=reason or None)
        return ToolResult(success=True, message="訊息已刪除")


class ReadImageTool(ToolBase):
    """讀取訊息中的圖片。"""

    name = "read_image"
    description = "讀取訊息中的圖片附件（需要模型支援視覺功能）"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
            "message_id": {"type": "string", "description": "訊息 ID"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, channel_id: str, message_id: str, **kwargs: Any) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在")
        msg = await channel.fetch_message(int(message_id))
        image_urls = [
            att.url for att in msg.attachments
            if att.content_type and att.content_type.startswith("image/")
        ]
        if not image_urls:
            return ToolResult(success=False, error="訊息中沒有圖片附件")
        return ToolResult(success=True, data={"image_urls": image_urls}, message="圖片 URL 已取得")


class SendDMTool(ToolBase):
    """發送私訊給成員。"""

    name = "send_dm"
    description = "發送私訊（DM）給指定成員"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "使用者 ID"},
            "content": {"type": "string", "description": "私訊內容"},
        },
        "required": ["user_id", "content"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, user_id: str, content: str, **kwargs: Any) -> ToolResult:
        user = await self._bot.fetch_user(int(user_id))
        if not user:
            return ToolResult(success=False, error=f"使用者 {user_id} 不存在")
        if user.bot:
            return ToolResult(success=False, error="無法發送私訊給 Bot")
        msg = await user.send(content)
        return ToolResult(success=True, data={"message_id": str(msg.id)}, message=f"私訊已發送給 {user.display_name}")


class SendEmbedTool(ToolBase):
    """發送嵌入式訊息。"""

    name = "send_embed"
    description = "發送嵌入式（Embed）訊息至指定頻道"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
            "title": {"type": "string", "description": "Embed 標題"},
            "description": {"type": "string", "description": "Embed 描述"},
            "color": {"type": "string", "description": "顏色 hex（如 #00FF00）"},
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "value": {"type": "string"},
                        "inline": {"type": "boolean"},
                    },
                    "required": ["name", "value"],
                },
                "description": "Embed 欄位列表",
            },
        },
        "required": ["channel_id", "title"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(
        self, channel_id: str, title: str, description: str = "",
        color: str = "", fields: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在")

        embed = discord.Embed(title=title, description=description or None)
        if color:
            try:
                embed.color = discord.Color(int(color.lstrip("#"), 16))
            except ValueError:
                pass
        if fields:
            for f in fields:
                embed.add_field(
                    name=f["name"], value=f["value"],
                    inline=f.get("inline", False),
                )

        msg = await channel.send(embed=embed)
        return ToolResult(success=True, data={"message_id": str(msg.id)}, message="Embed 訊息已發送")


class BulkDeleteMessagesTool(ToolBase):
    """批量刪除訊息。"""

    name = "bulk_delete_messages"
    description = "批量刪除頻道中的訊息（最多 100 條）"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
            "count": {"type": "integer", "description": "要刪除的訊息數量（1-100）"},
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["channel_id", "count"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, channel_id: str, count: int, reason: str = "", **kwargs: Any) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在")
        count = min(max(count, 1), 100)
        deleted = await channel.purge(limit=count, reason=reason or None)
        return ToolResult(success=True, data={"deleted_count": len(deleted)}, message=f"已刪除 {len(deleted)} 條訊息")


class PinMessageTool(ToolBase):
    """置頂訊息。"""

    name = "pin_message"
    description = "將指定訊息置頂在頻道"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
            "message_id": {"type": "string", "description": "訊息 ID"},
            "reason": {"type": "string", "description": "置頂原因"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, channel_id: str, message_id: str, reason: str = "", **kwargs: Any) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在")
        msg = await channel.fetch_message(int(message_id))
        await msg.pin(reason=reason or None)
        return ToolResult(success=True, message="訊息已置頂")


class UnpinMessageTool(ToolBase):
    """取消置頂訊息。"""

    name = "unpin_message"
    description = "取消指定訊息的置頂"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
            "message_id": {"type": "string", "description": "訊息 ID"},
            "reason": {"type": "string", "description": "取消置頂原因"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, channel_id: str, message_id: str, reason: str = "", **kwargs: Any) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在")
        msg = await channel.fetch_message(int(message_id))
        await msg.unpin(reason=reason or None)
        return ToolResult(success=True, message="訊息已取消置頂")


class GetPinnedMessagesTool(ToolBase):
    """取得置頂訊息列表。"""

    name = "get_pinned_messages"
    description = "取得頻道中所有置頂訊息"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID"},
        },
        "required": ["channel_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, channel_id: str, **kwargs: Any) -> ToolResult:
        channel = self._bot.get_channel(int(channel_id))
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error=f"頻道 {channel_id} 不存在")
        pinned = await channel.pins()
        data = [
            {"message_id": str(m.id), "author": m.author.display_name, "content": m.content[:200]}
            for m in pinned
        ]
        return ToolResult(success=True, data={"pinned_messages": data, "count": len(data)})

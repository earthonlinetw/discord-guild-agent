"""Discord 管理工具集合。

所有 Discord 操作皆封裝為 Tool，帶有安全等級標註。
Agent 只能透過 Tool 執行操作，確保行為可控。

安全等級：
- SAFE: 純讀取、低風險操作（讀取資訊、發送訊息）
- MODERATE: 可能影響他人的操作（編輯、靜音、建立頻道）
- DANGEROUS: 不可逆或高風險操作（刪除頻道、踢人、封禁）
"""

from __future__ import annotations

from typing import Any, Callable

import discord

import structlog

from src.tools.base import ToolBase, SafetyLevel, ToolResult

logger = structlog.get_logger(__name__)


# ============================================================
# 輔助函式
# ============================================================


def _guild(bot: discord.Client, guild_id: str) -> discord.Guild | None:
    """安全取得 Guild。"""
    try:
        gid = int(guild_id)
    except (ValueError, TypeError):
        return None
    return bot.get_guild(gid)


def _channel(bot: discord.Client, channel_id: str) -> discord.abc.GuildChannel | discord.Thread | None:
    """安全取得 Channel。"""
    try:
        cid = int(channel_id)
    except (ValueError, TypeError):
        return None
    return bot.get_channel(cid)


def _member(guild: discord.Guild, member_id: str) -> discord.Member | None:
    """安全取得 Member。"""
    try:
        mid = int(member_id)
    except (ValueError, TypeError):
        return None
    return guild.get_member(mid)


def _role(guild: discord.Guild, role_id: str) -> discord.Role | None:
    """安全取得 Role。"""
    try:
        rid = int(role_id)
    except (ValueError, TypeError):
        return None
    return guild.get_role(rid)


# 支援的權限名稱（對應 discord.Permissions 的屬性名稱）
VALID_PERMISSIONS = {
    # 一般
    "administrator", "view_audit_log", "manage_guild", "manage_roles",
    "manage_channels", "kick_members", "ban_members", "create_instant_invite",
    "change_nickname", "manage_nicknames", "manage_emojis_and_stickers",
    "manage_emojis", "manage_webhooks", "view_guild_insights",
    "moderate_members", "manage_events",
    # 文字
    "view_channel", "read_messages", "send_messages", "send_tts_messages",
    "manage_messages", "embed_links", "attach_files", "read_message_history",
    "mention_everyone", "use_external_emojis", "external_emojis",
    "add_reactions", "send_messages_in_threads", "create_public_threads",
    "create_private_threads", "manage_threads", "use_application_commands",
    "use_external_stickers",
    # 語音
    "connect", "speak", "stream", "mute_members", "deafen_members",
    "move_members", "use_voice_activation", "priority_speaker",
    "request_to_speak", "use_embedded_activities",
}


def _build_permissions(perm_names: list[str]) -> discord.Permissions:
    """從權限名稱列表建立 discord.Permissions 物件。"""
    perms = discord.Permissions.none()
    kwargs: dict[str, bool] = {}
    for name in perm_names:
        key = name.strip().lower()
        if key in VALID_PERMISSIONS and hasattr(perms, key):
            kwargs[key] = True
    perms.update(**kwargs)
    return perms


def _build_overwrite(
    allow: list[str] | None, deny: list[str] | None
) -> discord.PermissionOverwrite:
    """從 allow / deny 權限名稱列表建立 PermissionOverwrite。"""
    overwrite = discord.PermissionOverwrite()
    for name in allow or []:
        key = name.strip().lower()
        if key in VALID_PERMISSIONS and hasattr(overwrite, key):
            setattr(overwrite, key, True)
    for name in deny or []:
        key = name.strip().lower()
        if key in VALID_PERMISSIONS and hasattr(overwrite, key):
            setattr(overwrite, key, False)
    return overwrite


# 發送訊息時的 allowed_mentions 參數 schema（給每個發訊息工具共用）
ALLOWED_MENTIONS_SCHEMA = {
    "mention_users": {
        "type": "boolean",
        "description": "是否允許 @提及使用者（預設 true）",
    },
    "mention_roles": {
        "type": "boolean",
        "description": "是否允許 @提及身份組（預設 false，避免大量通知）",
    },
    "mention_everyone": {
        "type": "boolean",
        "description": "是否允許 @everyone / @here（預設 false，避免騷擾全體）",
    },
    "mention_replied_user": {
        "type": "boolean",
        "description": "回覆時是否 ping 被回覆的人（預設 true，僅 reply 類工具有效）",
    },
}


def _build_allowed_mentions(
    *,
    mention_users: bool = True,
    mention_roles: bool = False,
    mention_everyone: bool = False,
    mention_replied_user: bool = True,
) -> discord.AllowedMentions:
    """建立 AllowedMentions。

    預設政策：只允許 user mention 與 reply mention，
    關閉 @everyone / @here 與身份組提及，避免誤觸大量通知。
    """
    return discord.AllowedMentions(
        users=mention_users,
        roles=mention_roles,
        everyone=mention_everyone,
        replied_user=mention_replied_user,
    )


# ============================================================
# SAFE 工具 — 讀取 / 低風險操作
# ============================================================


class SendMessage(ToolBase):
    """發送訊息到指定頻道。"""

    name = "send_message"
    description = "發送訊息到指定頻道"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "content": {"type": "string", "description": "訊息內容"},
            **ALLOWED_MENTIONS_SCHEMA,
        },
        "required": ["channel_id", "content"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(
        self,
        *,
        channel_id: str,
        content: str,
        mention_users: bool = True,
        mention_roles: bool = False,
        mention_everyone: bool = False,
        mention_replied_user: bool = True,
    ) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="頻道不存在或類型不正確")
        allowed = _build_allowed_mentions(
            mention_users=mention_users,
            mention_roles=mention_roles,
            mention_everyone=mention_everyone,
            mention_replied_user=mention_replied_user,
        )
        msg = await channel.send(content, allowed_mentions=allowed)
        return ToolResult(success=True, data={"message_id": str(msg.id)}, message="訊息已發送")


class ReplyMessage(ToolBase):
    """回覆指定訊息。"""

    name = "reply_message"
    description = "回覆指定訊息"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "訊息 ID"},
            "content": {"type": "string", "description": "回覆內容"},
            **ALLOWED_MENTIONS_SCHEMA,
        },
        "required": ["channel_id", "message_id", "content"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(
        self,
        *,
        channel_id: str,
        message_id: str,
        content: str,
        mention_users: bool = True,
        mention_roles: bool = False,
        mention_everyone: bool = False,
        mention_replied_user: bool = True,
    ) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="頻道不存在或類型不正確")
        try:
            msg = await channel.fetch_message(int(message_id))
            allowed = _build_allowed_mentions(
                mention_users=mention_users,
                mention_roles=mention_roles,
                mention_everyone=mention_everyone,
                mention_replied_user=mention_replied_user,
            )
            reply = await msg.reply(content, allowed_mentions=allowed)
            return ToolResult(success=True, data={"message_id": str(reply.id)}, message="已回覆")
        except discord.NotFound:
            return ToolResult(success=False, error="訊息不存在")


class ReactionMessage(ToolBase):
    """對訊息添加表情反應。"""

    name = "add_reaction"
    description = "對指定訊息添加表情反應"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "訊息 ID"},
            "emoji": {"type": "string", "description": "表情符號（Unicode 或自訂表情 ID）"},
        },
        "required": ["channel_id", "message_id", "emoji"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, message_id: str, emoji: str) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="頻道不存在或類型不正確")
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.add_reaction(emoji)
            return ToolResult(success=True, message="反應已添加")
        except discord.NotFound:
            return ToolResult(success=False, error="訊息不存在")
        except discord.HTTPException as exc:
            return ToolResult(success=False, error=f"添加反應失敗: {exc}")


class ReadImage(ToolBase):
    """讀取訊息中的圖片附件 URL。"""

    name = "read_image"
    description = "讀取指定訊息中的圖片附件 URL"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "訊息 ID"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, message_id: str) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="頻道不存在或類型不正確")
        try:
            msg = await channel.fetch_message(int(message_id))
            images = [
                {"url": a.url, "filename": a.filename, "size": a.size}
                for a in msg.attachments
                if a.content_type and a.content_type.startswith("image/")
            ]
            return ToolResult(
                success=True,
                data={"images": images, "count": len(images)},
                message=f"找到 {len(images)} 張圖片",
            )
        except discord.NotFound:
            return ToolResult(success=False, error="訊息不存在")


class CreateThread(ToolBase):
    """從訊息建立討論串。"""

    name = "create_thread"
    description = "從指定訊息建立討論串（Thread）"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "來源訊息 ID"},
            "name": {"type": "string", "description": "討論串名稱"},
            "auto_archive_duration": {"type": "integer", "description": "自動封存時間（分鐘）", "enum": [60, 1440, 4320, 10080]},
        },
        "required": ["channel_id", "message_id", "name"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, message_id: str, name: str, auto_archive_duration: int = 1440) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return ToolResult(success=False, error="頻道不存在或不是文字頻道")
        try:
            msg = await channel.fetch_message(int(message_id))
            thread = await msg.create_thread(name=name, auto_archive_duration=auto_archive_duration)
            return ToolResult(
                success=True,
                data={"thread_id": str(thread.id), "name": thread.name},
                message="討論串已建立",
            )
        except discord.HTTPException as exc:
            return ToolResult(success=False, error=f"建立討論串失敗: {exc}")


class ArchiveThread(ToolBase):
    """封存討論串。"""

    name = "archive_thread"
    description = "封存指定的討論串"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "thread_id": {"type": "string", "description": "討論串 ID"},
        },
        "required": ["thread_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, thread_id: str) -> ToolResult:
        channel = _channel(self._bot, thread_id)
        if not channel or not isinstance(channel, discord.Thread):
            return ToolResult(success=False, error="討論串不存在")
        await channel.edit(archived=True)
        return ToolResult(success=True, message="討論串已封存")


class GetServerInfo(ToolBase):
    """取得伺服器資訊。"""

    name = "get_server_info"
    description = "取得伺服器（Guild）的詳細資訊"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        return ToolResult(
            success=True,
            data={
                "id": str(guild.id),
                "name": guild.name,
                "member_count": guild.member_count,
                "channel_count": len(guild.channels),
                "role_count": len(guild.roles),
                "emoji_count": len(guild.emojis),
                "owner_id": str(guild.owner_id),
                "premium_tier": guild.premium_tier,
                "features": guild.features,
            },
        )


class GetChannelInfo(ToolBase):
    """取得頻道資訊。"""

    name = "get_channel_info"
    description = "取得頻道的詳細資訊"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
        },
        "required": ["channel_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel:
            return ToolResult(success=False, error="頻道不存在")
        data: dict[str, Any] = {
            "id": str(channel.id),
            "name": getattr(channel, "name", "N/A"),
            "type": str(channel.type),
        }
        if isinstance(channel, discord.TextChannel):
            data.update({
                "topic": channel.topic,
                "slowmode_delay": channel.slowmode_delay,
                "nsfw": channel.nsfw,
                "category": channel.category.name if channel.category else None,
            })
        if isinstance(channel, discord.VoiceChannel):
            data.update({
                "bitrate": channel.bitrate,
                "user_limit": channel.user_limit,
                "members_count": len(channel.members),
            })
        return ToolResult(success=True, data=data)


class GetMemberInfo(ToolBase):
    """取得成員資訊。"""

    name = "get_member_info"
    description = "取得伺服器成員的詳細資訊"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
        },
        "required": ["guild_id", "member_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        member = _member(guild, member_id)
        if not member:
            return ToolResult(success=False, error="成員不存在")
        return ToolResult(
            success=True,
            data={
                "id": str(member.id),
                "name": member.name,
                "display_name": member.display_name,
                "nickname": member.nick,
                "roles": [r.name for r in member.roles[1:]],  # skip @everyone
                "joined_at": str(member.joined_at) if member.joined_at else None,
                "is_bot": member.bot,
                "status": str(member.status),
                "avatar_url": str(member.display_avatar.url),
            },
        )


class SearchMessages(ToolBase):
    """搜尋頻道中的訊息。"""

    name = "search_messages"
    description = "搜尋頻道中包含特定關鍵字的訊息"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "query": {"type": "string", "description": "搜尋關鍵字"},
            "limit": {"type": "integer", "description": "回傳數量上限", "default": 10},
        },
        "required": ["channel_id", "query"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, query: str, limit: int = 10) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="頻道不存在或類型不正確")
        found = []
        async for msg in channel.history(limit=100):
            if query.lower() in msg.content.lower():
                found.append({
                    "id": str(msg.id),
                    "author": msg.author.display_name,
                    "content": msg.content[:200],
                    "created_at": str(msg.created_at),
                })
                if len(found) >= limit:
                    break
        return ToolResult(
            success=True,
            data={"messages": found, "count": len(found)},
            message=f"找到 {len(found)} 筆結果",
        )


class GetPinnedMessages(ToolBase):
    """取得頻道中的釘選訊息。"""

    name = "get_pinned_messages"
    description = "取得頻道中所有釘選訊息"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
        },
        "required": ["channel_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return ToolResult(success=False, error="頻道不存在或不是文字頻道")
        pins = await channel.pins()
        data = [
            {
                "id": str(m.id),
                "author": m.author.display_name,
                "content": m.content[:300],
                "pinned_at": str(m.created_at),
            }
            for m in pins
        ]
        return ToolResult(success=True, data={"messages": data, "count": len(data)})


class ListRoles(ToolBase):
    """列出伺服器所有角色。"""

    name = "list_roles"
    description = "列出伺服器中所有角色"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        roles = [
            {
                "id": str(r.id),
                "name": r.name,
                "color": str(r.color),
                "member_count": len(r.members),
                "permissions": str(r.permissions.value),
                "is_default": r.is_default(),
                "is_bot_managed": r.is_bot_managed(),
                "position": r.position,
            }
            for r in guild.roles
        ]
        return ToolResult(success=True, data={"roles": roles, "count": len(roles)})


class ListMembers(ToolBase):
    """列出伺服器成員。"""

    name = "list_members"
    description = "列出伺服器中的成員（支援分頁）"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "limit": {"type": "integer", "description": "回傳數量上限", "default": 25},
            "after": {"type": "string", "description": "從此 ID 之後開始（分頁用）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, limit: int = 25, after: str = "0") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        members = [
            {
                "id": str(m.id),
                "name": m.name,
                "display_name": m.display_name,
                "nickname": m.nick,
                "is_bot": m.bot,
                "status": str(m.status),
            }
            for m in guild.members[:limit]
        ]
        return ToolResult(success=True, data={"members": members, "count": len(members)})


class ListActiveThreads(ToolBase):
    """列出伺服器中活躍的討論串。"""

    name = "list_active_threads"
    description = "列出伺服器中活躍的討論串"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        threads = await guild.active_threads()
        data = [
            {
                "id": str(t.id),
                "name": t.name,
                "parent_id": str(t.parent_id) if t.parent_id else None,
                "member_count": t.member_count,
                "message_count": t.message_count,
                "owner_id": str(t.owner_id),
            }
            for t in threads
        ]
        return ToolResult(success=True, data={"threads": data, "count": len(data)})


class GetAuditLog(ToolBase):
    """查詢伺服器審計日誌。"""

    name = "get_audit_log"
    description = "查詢伺服器的審計日誌（Audit Log）"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "action_type": {"type": "string", "description": "篩選動作類型（如 MEMBER_KICK, CHANNEL_CREATE）"},
            "limit": {"type": "integer", "description": "回傳數量上限", "default": 10},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, action_type: str | None = None, limit: int = 10) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            action = None
            if action_type:
                try:
                    action = discord.AuditLogAction[action_type.strip()]
                except KeyError:
                    return ToolResult(success=False, error=f"action_type '{action_type}' 無效，請使用 Discord AuditLogAction 名稱")

            if action is not None:
                entries = [entry async for entry in guild.audit_logs(limit=limit, action=action)]
            else:
                entries = [entry async for entry in guild.audit_logs(limit=limit)]

            data = [
                {
                    "id": str(e.id),
                    "action": str(e.action),
                    "user": str(e.user) if e.user else None,
                    "target": str(e.target) if e.target else None,
                    "reason": e.reason,
                    "created_at": str(e.created_at),
                }
                for e in entries
            ]
            return ToolResult(success=True, data={"entries": data, "count": len(data)})
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有查看審計日誌的權限")


class CreateInvite(ToolBase):
    """建立邀請連結。"""

    name = "create_invite"
    description = "為指定頻道建立邀請連結"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "max_uses": {"type": "integer", "description": "最大使用次數（0 為無限）", "default": 0},
            "max_age": {"type": "integer", "description": "有效時間（秒，0 為永不過期）", "default": 86400},
            "temporary": {"type": "boolean", "description": "是否為臨時成員", "default": False},
        },
        "required": ["channel_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, max_uses: int = 0, max_age: int = 86400, temporary: bool = False) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.abc.Invitable):
            return ToolResult(success=False, error="頻道不存在或不支援邀請")
        try:
            invite = await channel.create_invite(
                max_uses=max_uses, max_age=max_age, temporary=temporary
            )
            return ToolResult(
                success=True,
                data={"url": str(invite), "code": invite.code, "max_uses": max_uses, "max_age": max_age},
                message=f"邀請已建立: {invite}",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立邀請的權限")


class ListInvites(ToolBase):
    """列出伺服器邀請。"""

    name = "list_invites"
    description = "列出伺服器中所有邀請連結"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            invites = await guild.invites()
            data = [
                {
                    "code": i.code,
                    "url": str(i),
                    "inviter": str(i.inviter) if i.inviter else None,
                    "uses": i.uses,
                    "max_uses": i.max_uses,
                    "channel": str(i.channel) if i.channel else None,
                    "temporary": i.temporary,
                }
                for i in invites
            ]
            return ToolResult(success=True, data={"invites": data, "count": len(data)})
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有查看邀請的權限")


class ListEmojis(ToolBase):
    """列出伺服器自訂表情。"""

    name = "list_emojis"
    description = "列出伺服器中所有自訂表情"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        emojis = [
            {
                "id": str(e.id),
                "name": e.name,
                "animated": e.animated,
                "available": e.available,
                "managed": e.managed,
            }
            for e in guild.emojis
        ]
        return ToolResult(success=True, data={"emojis": emojis, "count": len(emojis)})


class ListWebhooks(ToolBase):
    """列出頻道 Webhook。"""

    name = "list_webhooks"
    description = "列出指定頻道中的 Webhook"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
        },
        "required": ["channel_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return ToolResult(success=False, error="頻道不存在或不是文字頻道")
        try:
            webhooks = await channel.webhooks()
            data = [
                {
                    "id": str(w.id),
                    "name": w.name,
                    "channel_id": str(w.channel_id),
                    "owner": str(w.user) if w.user else None,
                    "avatar_url": str(w.display_avatar.url) if w.display_avatar else None,
                }
                for w in webhooks
            ]
            return ToolResult(success=True, data={"webhooks": data, "count": len(data)})
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有查看 Webhook 的權限")


class GetVoiceParticipants(ToolBase):
    """取得語音頻道中的成員。"""

    name = "get_voice_participants"
    description = "取得語音頻道中目前的成員列表"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "語音頻道 ID（純數字字串）"},
        },
        "required": ["channel_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            return ToolResult(success=False, error="頻道不存在或不是語音頻道")
        members = [
            {
                "id": str(m.id),
                "name": m.name,
                "display_name": m.display_name,
                "self_mute": m.voice.self_mute if m.voice else None,
                "self_deaf": m.voice.self_deaf if m.voice else None,
            }
            for m in channel.members
        ]
        return ToolResult(success=True, data={"members": members, "count": len(members)})


class ListScheduledEvents(ToolBase):
    """列出伺服器排程活動。"""

    name = "list_scheduled_events"
    description = "列出伺服器中的排程活動（Scheduled Events）"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            events = await guild.fetch_scheduled_events()
            data = [
                {
                    "id": str(e.id),
                    "name": e.name,
                    "description": e.description,
                    "start_time": str(e.start_time) if e.start_time else None,
                    "end_time": str(e.end_time) if e.end_time else None,
                    "status": str(e.status),
                    "creator": str(e.creator) if e.creator else None,
                    "user_count": e.user_count,
                }
                for e in events
            ]
            return ToolResult(success=True, data={"events": data, "count": len(data)})
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有查看排程活動的權限")


class ListBans(ToolBase):
    """列出伺服器封禁清單。"""

    name = "list_bans"
    description = "列出伺服器中被封禁的使用者"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "limit": {"type": "integer", "description": "回傳數量上限", "default": 25},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, limit: int = 25) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            bans = [entry async for entry in guild.bans(limit=limit)]
            data = [
                {
                    "user_id": str(b.user.id),
                    "user_name": b.user.name,
                    "reason": b.reason,
                }
                for b in bans
            ]
            return ToolResult(success=True, data={"bans": data, "count": len(data)})
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有查看封禁清單的權限")


# ============================================================
# MODERATE 工具 — 可能影響他人的操作
# ============================================================


class EditMessage(ToolBase):
    """編輯訊息。"""

    name = "edit_message"
    description = "編輯自己發送的訊息"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "訊息 ID"},
            "content": {"type": "string", "description": "新內容"},
            **{k: v for k, v in ALLOWED_MENTIONS_SCHEMA.items() if k != "mention_replied_user"},
        },
        "required": ["channel_id", "message_id", "content"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(
        self,
        *,
        channel_id: str,
        message_id: str,
        content: str,
        mention_users: bool = True,
        mention_roles: bool = False,
        mention_everyone: bool = False,
    ) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="頻道不存在或類型不正確")
        try:
            msg = await channel.fetch_message(int(message_id))
            allowed = _build_allowed_mentions(
                mention_users=mention_users,
                mention_roles=mention_roles,
                mention_everyone=mention_everyone,
            )
            await msg.edit(content=content, allowed_mentions=allowed)
            return ToolResult(success=True, message="訊息已編輯")
        except discord.NotFound:
            return ToolResult(success=False, error="訊息不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="無法編輯此訊息（可能不是自己發的）")


class DeleteMessage(ToolBase):
    """刪除訊息。"""

    name = "delete_message"
    description = "刪除指定訊息"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "訊息 ID"},
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, message_id: str, reason: str = "") -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="頻道不存在或類型不正確")
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.delete(reason=reason or None)
            return ToolResult(success=True, message="訊息已刪除")
        except discord.NotFound:
            return ToolResult(success=False, error="訊息不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有刪除訊息的權限")


class BulkDeleteMessages(ToolBase):
    """批次刪除訊息。"""

    name = "bulk_delete_messages"
    description = "批次刪除頻道中的多筆訊息（最多 100 筆）"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要刪除的訊息 ID 列表（最多 100 筆）",
            },
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["channel_id", "message_ids"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, message_ids: list[str], reason: str = "") -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return ToolResult(success=False, error="頻道不存在或不是文字頻道")
        if len(message_ids) > 100:
            return ToolResult(success=False, error="最多只能刪除 100 筆訊息")
        try:
            msgs = []
            for mid in message_ids:
                try:
                    msg = await channel.fetch_message(int(mid))
                    msgs.append(msg)
                except discord.NotFound:
                    continue
            if not msgs:
                return ToolResult(success=False, error="找不到任何訊息")
            deleted = await channel.delete_messages(msgs, reason=reason or None)
            return ToolResult(success=True, data={"deleted_count": len(msgs)}, message=f"已刪除 {len(msgs)} 筆訊息")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有批次刪除的權限")
        except discord.HTTPException as exc:
            return ToolResult(success=False, error=f"批次刪除失敗: {exc}")


class PinMessage(ToolBase):
    """釘選訊息。"""

    name = "pin_message"
    description = "釘選指定訊息到頻道"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "訊息 ID"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, message_id: str) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return ToolResult(success=False, error="頻道不存在或不是文字頻道")
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.pin()
            return ToolResult(success=True, message="訊息已釘選")
        except discord.NotFound:
            return ToolResult(success=False, error="訊息不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有釘選訊息的權限")


class UnpinMessage(ToolBase):
    """取消釘選訊息。"""

    name = "unpin_message"
    description = "取消釘選指定訊息"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "訊息 ID"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, message_id: str) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return ToolResult(success=False, error="頻道不存在或不是文字頻道")
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.unpin()
            return ToolResult(success=True, message="訊息已取消釘選")
        except discord.NotFound:
            return ToolResult(success=False, error="訊息不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有取消釘選的權限")


class SendDM(ToolBase):
    """發送私訊。"""

    name = "send_dm"
    description = "發送私訊給指定使用者"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "使用者 ID"},
            "content": {"type": "string", "description": "訊息內容"},
            "mention_users": {"type": "boolean", "description": "是否允許 @提及使用者（預設 true）"},
        },
        "required": ["user_id", "content"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, user_id: str, content: str, mention_users: bool = True) -> ToolResult:
        user = self._bot.get_user(int(user_id))
        if not user:
            return ToolResult(success=False, error="使用者不存在")
        try:
            allowed = _build_allowed_mentions(mention_users=mention_users)
            await user.send(content, allowed_mentions=allowed)
            return ToolResult(success=True, message="私訊已發送")
        except discord.Forbidden:
            return ToolResult(success=False, error="無法發送私訊（使用者可能關閉了私訊）")


class CreateChannel(ToolBase):
    """建立文字頻道。"""

    name = "create_channel"
    description = "在伺服器中建立新的文字頻道"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "name": {"type": "string", "description": "頻道名稱"},
            "topic": {"type": "string", "description": "頻道主題"},
            "category_id": {"type": "string", "description": "分類 ID（可選）"},
        },
        "required": ["guild_id", "name"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, name: str, topic: str = "", category_id: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            kwargs: dict[str, Any] = {}
            if topic:
                kwargs["topic"] = topic
            if category_id:
                category = guild.get_channel(int(category_id))
                if isinstance(category, discord.CategoryChannel):
                    kwargs["category"] = category
            channel = await guild.create_text_channel(name, **kwargs)
            return ToolResult(
                success=True,
                data={"channel_id": str(channel.id), "name": channel.name},
                message=f"頻道 #{name} 已建立",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立頻道的權限")


class CreateVoiceChannel(ToolBase):
    """建立語音頻道。"""

    name = "create_voice_channel"
    description = "在伺服器中建立新的語音頻道"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "name": {"type": "string", "description": "頻道名稱"},
            "bitrate": {"type": "integer", "description": "位元率（bps）"},
            "user_limit": {"type": "integer", "description": "人數上限（0 為無限）"},
            "category_id": {"type": "string", "description": "分類 ID（可選）"},
        },
        "required": ["guild_id", "name"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, name: str, bitrate: int = 64000, user_limit: int = 0, category_id: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            kwargs: dict[str, Any] = {"bitrate": bitrate, "user_limit": user_limit}
            if category_id:
                category = guild.get_channel(int(category_id))
                if isinstance(category, discord.CategoryChannel):
                    kwargs["category"] = category
            channel = await guild.create_voice_channel(name, **kwargs)
            return ToolResult(
                success=True,
                data={"channel_id": str(channel.id), "name": channel.name},
                message=f"語音頻道 🔊{name} 已建立",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立語音頻道的權限")


class CreateCategory(ToolBase):
    """建立頻道分類。"""

    name = "create_category"
    description = "在伺服器中建立新的頻道分類（Category）"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "name": {"type": "string", "description": "分類名稱"},
        },
        "required": ["guild_id", "name"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, name: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            category = await guild.create_category(name)
            return ToolResult(
                success=True,
                data={"category_id": str(category.id), "name": category.name},
                message=f"分類 {name} 已建立",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立分類的權限")


class EditChannel(ToolBase):
    """編輯頻道設定。"""

    name = "edit_channel"
    description = "編輯頻道的設定（名稱、主題、移動到分類等）。適用文字、語音與分類頻道。"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "name": {"type": "string", "description": "新名稱（可選）"},
            "topic": {"type": "string", "description": "新主題（可選，僅文字頻道）"},
            "slowmode_delay": {"type": "integer", "description": "慢速模式延遲（秒，0 為關閉，僅文字頻道）"},
            "category_id": {"type": "string", "description": "把頻道移到此分類底下（純數字字串）。傳入空字串無作用；要移出分類請用 remove_from_category。"},
            "remove_from_category": {"type": "boolean", "description": "設為 true 會把頻道移出所有分類（變成獨立頻道）"},
            "position": {"type": "integer", "description": "頻道在列表中的位置（數字越小越上面）"},
        },
        "required": ["channel_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(
        self,
        *,
        channel_id: str,
        name: str = "",
        topic: str = "",
        slowmode_delay: int | None = None,
        category_id: str = "",
        remove_from_category: bool = False,
        position: int | None = None,
    ) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.abc.GuildChannel):
            return ToolResult(success=False, error="頻道不存在")
        try:
            kwargs: dict[str, Any] = {}
            if name:
                kwargs["name"] = name
            if topic and isinstance(channel, discord.TextChannel):
                kwargs["topic"] = topic
            if slowmode_delay is not None and isinstance(channel, discord.TextChannel):
                kwargs["slowmode_delay"] = slowmode_delay
            if position is not None:
                kwargs["position"] = position
            if remove_from_category:
                kwargs["category"] = None
            elif category_id:
                cat = channel.guild.get_channel(int(category_id))
                if isinstance(cat, discord.CategoryChannel):
                    kwargs["category"] = cat
                else:
                    return ToolResult(success=False, error="指定的 category_id 不是有效的分類")
            await channel.edit(**kwargs)
            return ToolResult(success=True, message="頻道已更新")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有編輯頻道的權限")
        except ValueError:
            return ToolResult(success=False, error="category_id 必須是純數字字串")


class SetChannelSlowMode(ToolBase):
    """設定頻道慢速模式。"""

    name = "set_channel_slowmode"
    description = "設定頻道的慢速模式（限制發言頻率）"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "delay": {"type": "integer", "description": "慢速模式延遲秒數（0 = 關閉，最大 21600）"},
        },
        "required": ["channel_id", "delay"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, delay: int) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return ToolResult(success=False, error="頻道不存在或不是文字頻道")
        try:
            await channel.edit(slowmode_delay=delay)
            status = f"{delay} 秒" if delay > 0 else "關閉"
            return ToolResult(success=True, message=f"慢速模式已設為 {status}")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有設定慢速模式的權限")


class EditThread(ToolBase):
    """編輯討論串。"""

    name = "edit_thread"
    description = "編輯討論串的設定（名稱、自動封存時間等）"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "thread_id": {"type": "string", "description": "討論串 ID"},
            "name": {"type": "string", "description": "新名稱（可選）"},
            "auto_archive_duration": {"type": "integer", "description": "自動封存時間（分鐘）", "enum": [60, 1440, 4320, 10080]},
            "slowmode_delay": {"type": "integer", "description": "慢速模式延遲（秒）"},
        },
        "required": ["thread_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, thread_id: str, name: str = "", auto_archive_duration: int | None = None, slowmode_delay: int | None = None) -> ToolResult:
        channel = _channel(self._bot, thread_id)
        if not channel or not isinstance(channel, discord.Thread):
            return ToolResult(success=False, error="討論串不存在")
        try:
            kwargs: dict[str, Any] = {}
            if name:
                kwargs["name"] = name
            if auto_archive_duration is not None:
                kwargs["auto_archive_duration"] = auto_archive_duration
            if slowmode_delay is not None:
                kwargs["slowmode_delay"] = slowmode_delay
            await channel.edit(**kwargs)
            return ToolResult(success=True, message="討論串已更新")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有編輯討論串的權限")


class UnarchiveThread(ToolBase):
    """取消封存討論串。"""

    name = "unarchive_thread"
    description = "取消封存已歸檔的討論串"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "thread_id": {"type": "string", "description": "討論串 ID"},
        },
        "required": ["thread_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, thread_id: str) -> ToolResult:
        channel = _channel(self._bot, thread_id)
        if not channel or not isinstance(channel, discord.Thread):
            return ToolResult(success=False, error="討論串不存在")
        try:
            await channel.edit(archived=False)
            return ToolResult(success=True, message="討論串已取消封存")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有取消封存的權限")


class CreateRole(ToolBase):
    """建立角色。"""

    name = "create_role"
    description = "在伺服器中建立新角色，可同時設定權限"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "name": {"type": "string", "description": "角色名稱"},
            "color": {"type": "string", "description": "角色顏色（hex，如 #FF0000）"},
            "hoist": {"type": "boolean", "description": "是否在成員列表中分開顯示", "default": False},
            "mentionable": {"type": "boolean", "description": "是否可被 @提及", "default": False},
            "permissions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "權限名稱列表（英文小寫，如 send_messages, manage_messages, kick_members, manage_channels, administrator 等）。可用 list_permissions 工具查詢所有可用權限名稱。",
            },
        },
        "required": ["guild_id", "name"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, name: str, color: str = "", hoist: bool = False, mentionable: bool = False, permissions: list[str] | None = None) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            kwargs: dict[str, Any] = {"name": name, "hoist": hoist, "mentionable": mentionable}
            if color:
                kwargs["color"] = discord.Color(int(color.lstrip("#"), 16))
            if permissions:
                kwargs["permissions"] = _build_permissions(permissions)
            role = await guild.create_role(**kwargs)
            return ToolResult(
                success=True,
                data={"role_id": str(role.id), "name": role.name},
                message=f"角色 @{name} 已建立",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立角色的權限")


class EditRole(ToolBase):
    """編輯角色設定。"""

    name = "edit_role"
    description = "編輯角色的設定（名稱、顏色、權限等）。可用 add_permissions/remove_permissions 增減個別權限，或用 set_permissions 完整覆寫權限。"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "role_id": {"type": "string", "description": "角色 ID"},
            "name": {"type": "string", "description": "新名稱（可選）"},
            "color": {"type": "string", "description": "新顏色（hex）"},
            "hoist": {"type": "boolean", "description": "是否分開顯示"},
            "mentionable": {"type": "boolean", "description": "是否可被 @提及"},
            "set_permissions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "完整覆寫角色權限為這個列表（會清掉原本的權限，只保留列出的）。權限名稱用英文小寫，如 send_messages, manage_messages。",
            },
            "add_permissions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "在現有權限上「新增」這些權限（保留原本的）。",
            },
            "remove_permissions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "從現有權限「移除」這些權限。",
            },
        },
        "required": ["guild_id", "role_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(
        self,
        *,
        guild_id: str,
        role_id: str,
        name: str = "",
        color: str = "",
        hoist: bool | None = None,
        mentionable: bool | None = None,
        set_permissions: list[str] | None = None,
        add_permissions: list[str] | None = None,
        remove_permissions: list[str] | None = None,
    ) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        role = _role(guild, role_id)
        if not role:
            return ToolResult(success=False, error="角色不存在")
        try:
            kwargs: dict[str, Any] = {}
            if name:
                kwargs["name"] = name
            if color:
                kwargs["color"] = discord.Color(int(color.lstrip("#"), 16))
            if hoist is not None:
                kwargs["hoist"] = hoist
            if mentionable is not None:
                kwargs["mentionable"] = mentionable

            # 權限處理
            if set_permissions is not None:
                kwargs["permissions"] = _build_permissions(set_permissions)
            elif add_permissions or remove_permissions:
                # 以現有權限為基礎調整
                perms = discord.Permissions(role.permissions.value)
                update: dict[str, bool] = {}
                for n in add_permissions or []:
                    key = n.strip().lower()
                    if key in VALID_PERMISSIONS and hasattr(perms, key):
                        update[key] = True
                for n in remove_permissions or []:
                    key = n.strip().lower()
                    if key in VALID_PERMISSIONS and hasattr(perms, key):
                        update[key] = False
                perms.update(**update)
                kwargs["permissions"] = perms

            await role.edit(**kwargs)
            return ToolResult(
                success=True,
                data={"role_id": str(role.id), "permissions_value": role.permissions.value},
                message="角色已更新",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有編輯角色的權限（可能 bot 角色低於目標角色）")


class AssignRole(ToolBase):
    """指派角色給成員。"""

    name = "assign_role"
    description = "將角色指派給指定成員"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
            "role_id": {"type": "string", "description": "角色 ID"},
            "reason": {"type": "string", "description": "指派原因"},
        },
        "required": ["guild_id", "member_id", "role_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str, role_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        member = _member(guild, member_id)
        if not member:
            return ToolResult(success=False, error="成員不存在")
        role = _role(guild, role_id)
        if not role:
            return ToolResult(success=False, error="角色不存在")
        try:
            await member.add_roles(role, reason=reason or None)
            return ToolResult(success=True, message=f"已將角色 @{role.name} 指派給 {member.display_name}")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有指派角色的權限")


class RemoveRole(ToolBase):
    """移除成員的角色。"""

    name = "remove_role"
    description = "移除指定成員的角色"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
            "role_id": {"type": "string", "description": "角色 ID"},
            "reason": {"type": "string", "description": "移除原因"},
        },
        "required": ["guild_id", "member_id", "role_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str, role_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        member = _member(guild, member_id)
        if not member:
            return ToolResult(success=False, error="成員不存在")
        role = _role(guild, role_id)
        if not role:
            return ToolResult(success=False, error="角色不存在")
        try:
            await member.remove_roles(role, reason=reason or None)
            return ToolResult(success=True, message=f"已移除 {member.display_name} 的角色 @{role.name}")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有移除角色的權限")


class ChangeNickname(ToolBase):
    """修改成員暱稱。"""

    name = "change_nickname"
    description = "修改伺服器成員的暱稱"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
            "nickname": {"type": "string", "description": "新暱稱（空字串清除暱稱）"},
        },
        "required": ["guild_id", "member_id", "nickname"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str, nickname: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        member = _member(guild, member_id)
        if not member:
            return ToolResult(success=False, error="成員不存在")
        try:
            await member.edit(nick=nickname or None)
            return ToolResult(success=True, message=f"暱稱已修改為「{nickname}」")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有修改暱稱的權限")


class MuteMember(ToolBase):
    """伺服器靜音成員。"""

    name = "mute_member"
    description = "在伺服器中靜音指定成員（Server Mute）"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
            "reason": {"type": "string", "description": "靜音原因"},
        },
        "required": ["guild_id", "member_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        member = _member(guild, member_id)
        if not member:
            return ToolResult(success=False, error="成員不存在")
        try:
            await member.edit(mute=True, reason=reason or None)
            return ToolResult(success=True, message=f"已靜音 {member.display_name}")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有靜音的權限")


class UnmuteMember(ToolBase):
    """取消伺服器靜音。"""

    name = "unmute_member"
    description = "取消成員的伺服器靜音"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
            "reason": {"type": "string", "description": "取消靜音原因"},
        },
        "required": ["guild_id", "member_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        member = _member(guild, member_id)
        if not member:
            return ToolResult(success=False, error="成員不存在")
        try:
            await member.edit(mute=False, reason=reason or None)
            return ToolResult(success=True, message=f"已取消靜音 {member.display_name}")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有取消靜音的權限")


class TimeoutMember(ToolBase):
    """Timeout 成員。"""

    name = "timeout_member"
    description = "將成員放入隔離（Timeout），直到指定時間"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
            "duration_seconds": {"type": "integer", "description": "Timeout 時長（秒）", "default": 60},
            "reason": {"type": "string", "description": "Timeout 原因"},
        },
        "required": ["guild_id", "member_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str, duration_seconds: int = 60, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        member = _member(guild, member_id)
        if not member:
            return ToolResult(success=False, error="成員不存在")
        from datetime import timedelta, datetime, timezone
        until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
        try:
            await member.edit(timed_out_until=until, reason=reason or None)
            return ToolResult(
                success=True,
                data={"timed_out_until": str(until)},
                message=f"已 Timeout {member.display_name} 直到 {until}",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有 Timeout 的權限")


class RemoveTimeout(ToolBase):
    """移除成員的 Timeout。"""

    name = "remove_timeout"
    description = "移除成員的 Timeout 狀態"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
            "reason": {"type": "string", "description": "移除 Timeout 原因"},
        },
        "required": ["guild_id", "member_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        member = _member(guild, member_id)
        if not member:
            return ToolResult(success=False, error="成員不存在")
        try:
            await member.edit(timed_out_until=None, reason=reason or None)
            return ToolResult(success=True, message=f"已移除 {member.display_name} 的 Timeout")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有移除 Timeout 的權限")


class UnbanMember(ToolBase):
    """解除封禁。"""

    name = "unban_member"
    description = "解除指定使用者的伺服器封禁"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "user_id": {"type": "string", "description": "使用者 ID"},
            "reason": {"type": "string", "description": "解除封禁原因"},
        },
        "required": ["guild_id", "user_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, user_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            await guild.unban(discord.Object(int(user_id)), reason=reason or None)
            return ToolResult(success=True, message="已解除封禁")
        except discord.NotFound:
            return ToolResult(success=False, error="該使用者不在封禁清單中")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有解除封禁的權限")


class MoveMember(ToolBase):
    """移動成員到其他語音頻道。"""

    name = "move_member"
    description = "將成員從一個語音頻道移動到另一個"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
            "channel_id": {"type": "string", "description": "目標語音頻道 ID"},
            "reason": {"type": "string", "description": "移動原因"},
        },
        "required": ["guild_id", "member_id", "channel_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str, channel_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        member = _member(guild, member_id)
        if not member:
            return ToolResult(success=False, error="成員不存在")
        if not member.voice or not member.voice.channel:
            return ToolResult(success=False, error="成員不在語音頻道中")
        try:
            await member.move_to(_channel(self._bot, channel_id), reason=reason or None)  # type: ignore[arg-type]
            return ToolResult(success=True, message=f"已移動 {member.display_name}")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有移動成員的權限")


class DeleteInvite(ToolBase):
    """刪除邀請連結。"""

    name = "delete_invite"
    description = "撤銷指定的邀請連結"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "code": {"type": "string", "description": "邀請代碼"},
            "reason": {"type": "string", "description": "撤銷原因"},
        },
        "required": ["guild_id", "code"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, code: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            invites = await guild.invites()
            for invite in invites:
                if invite.code == code:
                    await invite.delete(reason=reason or None)
                    return ToolResult(success=True, message=f"邀請 {code} 已撤銷")
            return ToolResult(success=False, error=f"找不到邀請 {code}")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有管理邀請的權限")


class CreateWebhook(ToolBase):
    """建立 Webhook。"""

    name = "create_webhook"
    description = "在頻道中建立 Webhook"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "name": {"type": "string", "description": "Webhook 名稱"},
            "avatar_url": {"type": "string", "description": "頭像 URL（可選）"},
        },
        "required": ["channel_id", "name"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, name: str, avatar_url: str = "") -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return ToolResult(success=False, error="頻道不存在或不是文字頻道")
        try:
            kwargs: dict[str, Any] = {}
            if avatar_url:
                kwargs["avatar"] = avatar_url  # discord.py 會自動處理
            webhook = await channel.create_webhook(name=name, **kwargs)
            return ToolResult(
                success=True,
                data={"id": str(webhook.id), "name": webhook.name, "url": webhook.url},
                message=f"Webhook '{name}' 已建立",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立 Webhook 的權限")


class CreateScheduledEvent(ToolBase):
    """建立排程活動。"""

    name = "create_scheduled_event"
    description = "在伺服器中建立排程活動（Scheduled Event）"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "name": {"type": "string", "description": "活動名稱"},
            "description": {"type": "string", "description": "活動描述"},
            "start_time": {"type": "string", "description": "開始時間（ISO 8601 格式）"},
            "end_time": {"type": "string", "description": "結束時間（ISO 8601 格式，可選）"},
            "channel_id": {"type": "string", "description": "語音頻道 ID（可選）"},
            "location": {"type": "string", "description": "外部位置（可選）"},
        },
        "required": ["guild_id", "name", "start_time"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, name: str, start_time: str, description: str = "", end_time: str = "", channel_id: str = "", location: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            from datetime import datetime
            start_dt = datetime.fromisoformat(start_time)
            kwargs: dict[str, Any] = {"name": name, "start_time": start_dt}
            if description:
                kwargs["description"] = description
            if end_time:
                kwargs["end_time"] = datetime.fromisoformat(end_time)
            if channel_id:
                vc = guild.get_channel(int(channel_id))
                if isinstance(vc, discord.VoiceChannel):
                    kwargs["channel"] = vc
            elif location:
                kwargs["location"] = location

            event = await guild.create_scheduled_event(**kwargs)
            return ToolResult(
                success=True,
                data={"id": str(event.id), "name": event.name},
                message=f"活動 '{name}' 已建立",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立排程活動的權限")
        except (ValueError, TypeError) as exc:
            return ToolResult(success=False, error=f"參數錯誤: {exc}")


class CreateEmoji(ToolBase):
    """建立自訂表情。"""

    name = "create_emoji"
    description = "在伺服器中建立自訂表情"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "name": {"type": "string", "description": "表情名稱"},
            "image_url": {"type": "string", "description": "表情圖片 URL"},
            "roles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "限制使用的角色 ID 列表（可選）",
            },
        },
        "required": ["guild_id", "name", "image_url"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, name: str, image_url: str, roles: list[str] | None = None) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            role_objs = []
            if roles:
                for rid in roles:
                    r = guild.get_role(int(rid))
                    if r:
                        role_objs.append(r)
            emoji = await guild.create_custom_emoji(name=name, image=image_url, roles=role_objs)
            return ToolResult(
                success=True,
                data={"id": str(emoji.id), "name": emoji.name},
                message=f"表情 :{name}: 已建立",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立表情的權限")


# ============================================================
# DANGEROUS 工具 — 不可逆 / 高風險操作
# ============================================================


class DeleteChannel(ToolBase):
    """刪除頻道。"""

    name = "delete_channel"
    description = "刪除指定的頻道（不可逆）"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["channel_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, reason: str = "") -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel:
            return ToolResult(success=False, error="頻道不存在")
        try:
            await channel.delete(reason=reason or None)
            return ToolResult(success=True, message="頻道已刪除")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有刪除頻道的權限")


class DeleteRole(ToolBase):
    """刪除角色。"""

    name = "delete_role"
    description = "刪除指定的角色（不可逆）"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "role_id": {"type": "string", "description": "角色 ID"},
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["guild_id", "role_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, role_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        role = _role(guild, role_id)
        if not role:
            return ToolResult(success=False, error="角色不存在")
        try:
            await role.delete(reason=reason or None)
            return ToolResult(success=True, message=f"角色 @{role.name} 已刪除")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有刪除角色的權限")


class KickMember(ToolBase):
    """踢出成員。"""

    name = "kick_member"
    description = "將成員踢出伺服器"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
            "reason": {"type": "string", "description": "踢出原因"},
        },
        "required": ["guild_id", "member_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        member = _member(guild, member_id)
        if not member:
            return ToolResult(success=False, error="成員不存在")
        try:
            await member.kick(reason=reason or None)
            return ToolResult(success=True, message=f"已踢出 {member.display_name}")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有踢出成員的權限")


class BanMember(ToolBase):
    """封禁成員。"""

    name = "ban_member"
    description = "封禁指定成員（可選刪除歷史訊息）"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_id": {"type": "string", "description": "成員 ID（純數字字串）"},
            "delete_message_days": {"type": "integer", "description": "刪除幾天內的訊息（0-7）", "default": 0},
            "reason": {"type": "string", "description": "封禁原因"},
        },
        "required": ["guild_id", "member_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_id: str, delete_message_days: int = 0, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            await guild.ban(
                discord.Object(int(member_id)),
                delete_message_days=delete_message_days,
                reason=reason or None,
            )
            return ToolResult(success=True, message="已封禁")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有封禁的權限")


class EditGuild(ToolBase):
    """編輯伺服器設定。"""

    name = "edit_guild"
    description = "編輯伺服器設定（名稱等）"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "name": {"type": "string", "description": "新伺服器名稱（可選）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, name: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            kwargs: dict[str, Any] = {}
            if name:
                kwargs["name"] = name
            await guild.edit(**kwargs)
            return ToolResult(success=True, message="伺服器設定已更新")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有編輯伺服器的權限")


class DeleteEmoji(ToolBase):
    """刪除自訂表情。"""

    name = "delete_emoji"
    description = "刪除伺服器中的自訂表情（不可逆）"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "emoji_id": {"type": "string", "description": "表情 ID"},
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["guild_id", "emoji_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, emoji_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        emoji = guild.get_emoji(int(emoji_id))
        if not emoji:
            return ToolResult(success=False, error="表情不存在")
        try:
            await emoji.delete(reason=reason or None)
            return ToolResult(success=True, message=f"表情 :{emoji.name}: 已刪除")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有刪除表情的權限")


class DeleteScheduledEvent(ToolBase):
    """刪除排程活動。"""

    name = "delete_scheduled_event"
    description = "刪除伺服器中的排程活動"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "event_id": {"type": "string", "description": "活動 ID"},
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["guild_id", "event_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, event_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            event = await guild.fetch_scheduled_event(int(event_id))
            await event.delete(reason=reason or None)
            return ToolResult(success=True, message=f"活動 '{event.name}' 已刪除")
        except discord.NotFound:
            return ToolResult(success=False, error="活動不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有刪除活動的權限")


class DeleteWebhook(ToolBase):
    """刪除 Webhook。"""

    name = "delete_webhook"
    description = "刪除指定的 Webhook"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "webhook_id": {"type": "string", "description": "Webhook ID"},
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["webhook_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, webhook_id: str, reason: str = "") -> ToolResult:
        try:
            webhook = await self._bot.fetch_webhook(int(webhook_id))
            await webhook.delete(reason=reason or None)
            return ToolResult(success=True, message=f"Webhook '{webhook.name}' 已刪除")
        except discord.NotFound:
            return ToolResult(success=False, error="Webhook 不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有刪除 Webhook 的權限")


# ============================================================
# 權限 / 分類 相關工具
# ============================================================


class ListPermissions(ToolBase):
    """列出所有可用的權限名稱。"""

    name = "list_permissions"
    description = "列出所有可用的 Discord 權限名稱（給 create_role / edit_role / set_channel_permissions 使用）"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self) -> ToolResult:
        return ToolResult(
            success=True,
            data={"permissions": sorted(VALID_PERMISSIONS)},
            message="可用權限名稱（英文小寫）",
        )


class ListCategories(ToolBase):
    """列出伺服器所有頻道分類。"""

    name = "list_categories"
    description = "列出伺服器中所有頻道分類（Category）及其底下的頻道"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        categories = []
        for cat in guild.categories:
            categories.append({
                "category_id": str(cat.id),
                "name": cat.name,
                "position": cat.position,
                "channels": [
                    {"channel_id": str(ch.id), "name": ch.name, "type": str(ch.type)}
                    for ch in cat.channels
                ],
            })
        uncategorized = [
            {"channel_id": str(ch.id), "name": ch.name, "type": str(ch.type)}
            for ch in guild.channels
            if ch.category is None and not isinstance(ch, discord.CategoryChannel)
        ]
        return ToolResult(
            success=True,
            data={"categories": categories, "uncategorized": uncategorized},
            message=f"共 {len(categories)} 個分類",
        )


class SetChannelPermissions(ToolBase):
    """設定頻道對某身份組或成員的權限覆寫。"""

    name = "set_channel_permissions"
    description = (
        "設定某個頻道（或分類）對特定身份組／成員的權限覆寫（permission overwrite）。"
        "例如：讓某身份組在某頻道看不到、不能發言。"
        "allow 列表內的權限會被允許，deny 列表內的會被拒絕，未列出的維持繼承。"
        "若目標是分類，底下同步繼承的頻道也會受影響。"
    )
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "channel_id": {"type": "string", "description": "頻道或分類 ID（純數字字串）"},
            "target_type": {"type": "string", "enum": ["role", "member"], "description": "覆寫對象類型：role（身份組）或 member（成員）"},
            "target_id": {"type": "string", "description": "對象 ID：身份組 ID 或成員 ID（純數字字串）。要設定 @everyone 請填該伺服器的 everyone 角色 ID（通常等於 guild_id）。"},
            "allow": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要「允許」的權限名稱列表，如 view_channel, send_messages。",
            },
            "deny": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要「拒絕」的權限名稱列表，如 send_messages, view_channel。",
            },
            "reason": {"type": "string", "description": "操作原因"},
        },
        "required": ["guild_id", "channel_id", "target_type", "target_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(
        self,
        *,
        guild_id: str,
        channel_id: str,
        target_type: str,
        target_id: str,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        reason: str = "",
    ) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.abc.GuildChannel):
            return ToolResult(success=False, error="頻道不存在")

        target: discord.Role | discord.Member | None
        if target_type == "role":
            target = _role(guild, target_id)
        elif target_type == "member":
            target = _member(guild, target_id)
        else:
            return ToolResult(success=False, error="target_type 必須是 role 或 member")
        if target is None:
            return ToolResult(success=False, error="找不到指定的身份組或成員")

        try:
            overwrite = _build_overwrite(allow, deny)
            await channel.set_permissions(target, overwrite=overwrite, reason=reason or None)
            return ToolResult(
                success=True,
                message=f"已更新 #{channel.name} 對 {getattr(target, 'name', target_id)} 的權限覆寫",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有管理頻道權限的權限")


# ============================================================
# 順序 / 階層 相關工具
# ============================================================


class EditRolePosition(ToolBase):
    """調整身份組在階層中的順序（position）。"""

    name = "edit_role_position"
    description = (
        "調整身份組在伺服器階層中的順序（position）。position 數字越大越靠上層、權限越高。"
        "注意：bot 自己的最高身份組必須高於要移動的目標位置，且不能移動到比 bot 更高的位置。"
        "先用 list_roles 查看目前各身份組的 position。"
    )
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "role_id": {"type": "string", "description": "要移動的身份組 ID（純數字字串）"},
            "position": {"type": "integer", "description": "新的 position（整數，越大越上層；@everyone 為 0）"},
            "reason": {"type": "string", "description": "操作原因"},
        },
        "required": ["guild_id", "role_id", "position"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, role_id: str, position: int, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        role = _role(guild, role_id)
        if not role:
            return ToolResult(success=False, error="身份組不存在")
        try:
            await guild.edit_role_positions(positions={role: position}, reason=reason or None)
            return ToolResult(
                success=True,
                data={"role_id": str(role.id), "position": role.position},
                message=f"已將身份組 @{role.name} 移動到 position {position}",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有調整身份組順序的權限（bot 角色須高於目標）")
        except discord.HTTPException as exc:
            return ToolResult(success=False, error=f"調整失敗：{exc}")


class EditChannelPosition(ToolBase):
    """調整頻道（或分類）在列表中的順序。"""

    name = "edit_channel_position"
    description = (
        "調整頻道或分類在伺服器列表中的顯示順序（position）。position 數字越小越靠上。"
        "分類本身也是頻道，可用此工具調整分類的順序。"
        "可選 sync_permissions=true 讓頻道同步其所屬分類的權限。"
    )
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道或分類 ID（純數字字串）"},
            "position": {"type": "integer", "description": "新的 position（整數，越小越上面）"},
            "sync_permissions": {"type": "boolean", "description": "是否同步所屬分類的權限（預設 false）"},
            "reason": {"type": "string", "description": "操作原因"},
        },
        "required": ["channel_id", "position"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, position: int, sync_permissions: bool = False, reason: str = "") -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.abc.GuildChannel):
            return ToolResult(success=False, error="頻道不存在")
        try:
            kwargs: dict[str, Any] = {"position": position}
            if sync_permissions:
                kwargs["sync_permissions"] = True
            await channel.edit(reason=reason or None, **kwargs)
            return ToolResult(
                success=True,
                data={"channel_id": str(channel.id), "position": channel.position},
                message=f"已將 #{channel.name} 移動到 position {position}",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有調整頻道順序的權限")


# ============================================================
# AutoMod 自動審核相關工具
# ============================================================

# AutoMod 觸發類型對照（給 AI 用的簡單名稱）
AUTOMOD_TRIGGERS = {
    "keyword": discord.AutoModRuleTriggerType.keyword,
    "spam": discord.AutoModRuleTriggerType.spam,
    "keyword_preset": discord.AutoModRuleTriggerType.keyword_preset,
    "mention_spam": discord.AutoModRuleTriggerType.mention_spam,
    "harmful_link": discord.AutoModRuleTriggerType.harmful_link,
}


def _automod_actions(
    guild: discord.Guild,
    block: bool,
    timeout_seconds: int,
    alert_channel_id: str,
    custom_message: str,
) -> list[discord.AutoModRuleAction]:
    """根據參數組合出 AutoMod 動作列表。"""
    import datetime

    actions: list[discord.AutoModRuleAction] = []
    if block:
        if custom_message:
            actions.append(discord.AutoModRuleAction(custom_message=custom_message))
        else:
            actions.append(
                discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)
            )
    if timeout_seconds > 0:
        actions.append(
            discord.AutoModRuleAction(duration=datetime.timedelta(seconds=timeout_seconds))
        )
    if alert_channel_id:
        try:
            actions.append(discord.AutoModRuleAction(channel_id=int(alert_channel_id)))
        except (ValueError, TypeError):
            pass
    if not actions:
        # 至少要有一個動作，預設封鎖訊息
        actions.append(
            discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)
        )
    return actions


class ListAutoModRules(ToolBase):
    """列出伺服器所有 AutoMod 規則。"""

    name = "list_automod_rules"
    description = "列出伺服器中所有 AutoMod（自動審核）規則及其設定"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            rules = await guild.fetch_automod_rules()
            data = []
            for r in rules:
                data.append({
                    "rule_id": str(r.id),
                    "name": r.name,
                    "enabled": r.enabled,
                    "trigger_type": r.trigger.type.name if r.trigger and r.trigger.type else None,
                    "keyword_filter": list(r.trigger.keyword_filter or []) if r.trigger else [],
                    "actions": [a.type.name for a in r.actions],
                })
            return ToolResult(success=True, data={"rules": data}, message=f"共 {len(data)} 條 AutoMod 規則")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有檢視 AutoMod 規則的權限")


class CreateAutoModRule(ToolBase):
    """建立 AutoMod 規則。"""

    name = "create_automod_rule"
    description = (
        "建立 AutoMod 自動審核規則。常用於關鍵字過濾、防洗版、防 mention 轟炸。"
        "trigger_type 可選：keyword（關鍵字，需提供 keywords）、spam（垃圾訊息）、"
        "mention_spam（提及轟炸，需提供 mention_limit）、keyword_preset（預設清單）、harmful_link（惡意連結）。"
    )
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "name": {"type": "string", "description": "規則名稱"},
            "trigger_type": {
                "type": "string",
                "enum": ["keyword", "spam", "mention_spam", "keyword_preset", "harmful_link"],
                "description": "觸發類型",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "關鍵字列表（trigger_type=keyword 時必填）。支援 * 萬用字元，如 *badword*",
            },
            "mention_limit": {"type": "integer", "description": "提及上限（trigger_type=mention_spam 時必填，1-50）"},
            "block": {"type": "boolean", "description": "是否封鎖違規訊息（預設 true）", "default": True},
            "timeout_seconds": {"type": "integer", "description": "違規者禁言秒數（0 = 不禁言）"},
            "alert_channel_id": {"type": "string", "description": "警報通知頻道 ID（可選，純數字字串）"},
            "custom_message": {"type": "string", "description": "封鎖時顯示給使用者的自訂訊息（可選）"},
            "enabled": {"type": "boolean", "description": "建立後是否立即啟用（預設 true）", "default": True},
        },
        "required": ["guild_id", "name", "trigger_type"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(
        self,
        *,
        guild_id: str,
        name: str,
        trigger_type: str,
        keywords: list[str] | None = None,
        mention_limit: int = 0,
        block: bool = True,
        timeout_seconds: int = 0,
        alert_channel_id: str = "",
        custom_message: str = "",
        enabled: bool = True,
    ) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")

        trig_type = AUTOMOD_TRIGGERS.get(trigger_type)
        if trig_type is None:
            return ToolResult(success=False, error=f"不支援的 trigger_type：{trigger_type}")

        # 建立 trigger
        try:
            if trigger_type == "keyword":
                if not keywords:
                    return ToolResult(success=False, error="trigger_type=keyword 必須提供 keywords")
                trigger = discord.AutoModTrigger(type=trig_type, keyword_filter=keywords)
            elif trigger_type == "mention_spam":
                if mention_limit <= 0:
                    return ToolResult(success=False, error="trigger_type=mention_spam 必須提供 mention_limit")
                trigger = discord.AutoModTrigger(type=trig_type, mention_limit=mention_limit)
            elif trigger_type == "keyword_preset":
                trigger = discord.AutoModTrigger(
                    type=trig_type,
                    presets=discord.AutoModPresets.all(),
                )
            else:  # spam / harmful_link
                trigger = discord.AutoModTrigger(type=trig_type)

            actions = _automod_actions(guild, block, timeout_seconds, alert_channel_id, custom_message)

            rule = await guild.create_automod_rule(
                name=name,
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=enabled,
            )
            return ToolResult(
                success=True,
                data={"rule_id": str(rule.id), "name": rule.name},
                message=f"AutoMod 規則「{name}」已建立",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立 AutoMod 規則的權限")
        except discord.HTTPException as exc:
            return ToolResult(success=False, error=f"建立失敗：{exc}")


class EditAutoModRule(ToolBase):
    """編輯 AutoMod 規則。"""

    name = "edit_automod_rule"
    description = "編輯既有的 AutoMod 規則（改名稱、啟用/停用、更新關鍵字）。先用 list_automod_rules 取得 rule_id。"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "rule_id": {"type": "string", "description": "AutoMod 規則 ID（純數字字串）"},
            "name": {"type": "string", "description": "新名稱（可選）"},
            "enabled": {"type": "boolean", "description": "啟用或停用（可選）"},
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "更新關鍵字列表（僅 keyword 類型規則有效，可選）",
            },
        },
        "required": ["guild_id", "rule_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(
        self,
        *,
        guild_id: str,
        rule_id: str,
        name: str = "",
        enabled: bool | None = None,
        keywords: list[str] | None = None,
    ) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            rule = await guild.fetch_automod_rule(int(rule_id))
        except (discord.NotFound, ValueError):
            return ToolResult(success=False, error="AutoMod 規則不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有存取 AutoMod 規則的權限")

        try:
            kwargs: dict[str, Any] = {}
            if name:
                kwargs["name"] = name
            if enabled is not None:
                kwargs["enabled"] = enabled
            if keywords is not None and rule.trigger:
                new_trigger = discord.AutoModTrigger(
                    type=rule.trigger.type, keyword_filter=keywords
                )
                kwargs["trigger"] = new_trigger
            await rule.edit(**kwargs)
            return ToolResult(success=True, message=f"AutoMod 規則「{rule.name}」已更新")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有編輯 AutoMod 規則的權限")
        except discord.HTTPException as exc:
            return ToolResult(success=False, error=f"編輯失敗：{exc}")


class DeleteAutoModRule(ToolBase):
    """刪除 AutoMod 規則。"""

    name = "delete_automod_rule"
    description = "刪除指定的 AutoMod 規則（不可逆）。先用 list_automod_rules 取得 rule_id。"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "rule_id": {"type": "string", "description": "AutoMod 規則 ID（純數字字串）"},
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["guild_id", "rule_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, rule_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            rule = await guild.fetch_automod_rule(int(rule_id))
            await rule.delete(reason=reason or None)
            return ToolResult(success=True, message=f"AutoMod 規則「{rule.name}」已刪除")
        except (discord.NotFound, ValueError):
            return ToolResult(success=False, error="AutoMod 規則不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有刪除 AutoMod 規則的權限")


# ============================================================
# 進階頻道工具（Stage / Forum / History）
# ============================================================


class CreateStageChannel(ToolBase):
    """建立舞台頻道。"""

    name = "create_stage_channel"
    description = "建立舞台頻道（Stage Channel），用於演講、AMA 等活動"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "name": {"type": "string", "description": "頻道名稱"},
            "category_id": {"type": "string", "description": "分類 ID（可選，純數字字串）"},
        },
        "required": ["guild_id", "name"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, name: str, category_id: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            kwargs: dict[str, Any] = {}
            if category_id:
                cat = guild.get_channel(int(category_id))
                if isinstance(cat, discord.CategoryChannel):
                    kwargs["category"] = cat
            channel = await guild.create_stage_channel(name, **kwargs)
            return ToolResult(
                success=True,
                data={"channel_id": str(channel.id), "name": channel.name},
                message=f"舞台頻道 {name} 已建立",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立舞台頻道的權限")


class CreateForumChannel(ToolBase):
    """建立論壇頻道。"""

    name = "create_forum_channel"
    description = "建立論壇頻道（Forum Channel），成員可在其中開啟貼文討論串"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "name": {"type": "string", "description": "頻道名稱"},
            "topic": {"type": "string", "description": "頻道主題／發文指南（可選）"},
            "category_id": {"type": "string", "description": "分類 ID（可選，純數字字串）"},
        },
        "required": ["guild_id", "name"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, name: str, topic: str = "", category_id: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            kwargs: dict[str, Any] = {}
            if topic:
                kwargs["topic"] = topic
            if category_id:
                cat = guild.get_channel(int(category_id))
                if isinstance(cat, discord.CategoryChannel):
                    kwargs["category"] = cat
            channel = await guild.create_forum(name, **kwargs)
            return ToolResult(
                success=True,
                data={"channel_id": str(channel.id), "name": channel.name},
                message=f"論壇頻道 {name} 已建立",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有建立論壇頻道的權限")


class CreateForumPost(ToolBase):
    """在論壇頻道建立貼文。"""

    name = "create_forum_post"
    description = "在論壇頻道（Forum）中建立一篇新貼文（貼文本身是一個討論串）"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "論壇頻道 ID（純數字字串）"},
            "title": {"type": "string", "description": "貼文標題"},
            "content": {"type": "string", "description": "貼文內容"},
            **{k: v for k, v in ALLOWED_MENTIONS_SCHEMA.items() if k != "mention_replied_user"},
        },
        "required": ["channel_id", "title", "content"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(
        self,
        *,
        channel_id: str,
        title: str,
        content: str,
        mention_users: bool = True,
        mention_roles: bool = False,
        mention_everyone: bool = False,
    ) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.ForumChannel):
            return ToolResult(success=False, error="頻道不存在或不是論壇頻道")
        try:
            allowed = _build_allowed_mentions(
                mention_users=mention_users,
                mention_roles=mention_roles,
                mention_everyone=mention_everyone,
            )
            result = await channel.create_thread(name=title, content=content, allowed_mentions=allowed)
            thread = result.thread
            return ToolResult(
                success=True,
                data={"thread_id": str(thread.id), "title": thread.name},
                message=f"論壇貼文「{title}」已發佈",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有在論壇發文的權限")


class GetChannelHistory(ToolBase):
    """讀取頻道訊息歷史。"""

    name = "get_channel_history"
    description = "讀取指定頻道最近的訊息歷史（用於了解上下文）"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "limit": {"type": "integer", "description": "讀取訊息數量（預設 20，最大 100）"},
        },
        "required": ["channel_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, limit: int = 20) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="頻道不存在或不支援訊息歷史")
        from src.discord.message_parser import serialize_message
        limit = max(1, min(limit, 100))
        try:
            messages = []
            async for msg in channel.history(limit=limit):
                # 完整序列化（含 embed / components / 轉發 / 系統事件）
                messages.append(serialize_message(msg))
            messages.reverse()  # 由舊到新
            return ToolResult(success=True, data={"messages": messages}, message=f"讀取 {len(messages)} 則訊息")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有讀取訊息歷史的權限")


class GetMessage(ToolBase):
    """取得單一訊息內容。"""

    name = "get_message"
    description = "依訊息 ID 取得單一訊息的完整內容"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "訊息 ID（純數字字串）"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, message_id: str) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="頻道不存在")
        from src.discord.message_parser import serialize_message
        try:
            msg = await channel.fetch_message(int(message_id))
            # 完整序列化：含 embed / components(V2) / 轉發訊息 / 系統事件類型
            data = serialize_message(msg)
            data["pinned"] = msg.pinned
            data["reactions"] = [{"emoji": str(r.emoji), "count": r.count} for r in msg.reactions]
            return ToolResult(success=True, data=data, message="已取得訊息（完整內容）")
        except (discord.NotFound, ValueError):
            return ToolResult(success=False, error="訊息不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有讀取訊息的權限")


class CrosspostMessage(ToolBase):
    """發布公告頻道訊息。"""

    name = "crosspost_message"
    description = "將公告頻道（Announcement Channel）中的訊息發布給所有追蹤的伺服器"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "公告頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "要發布的訊息 ID（純數字字串）"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, channel_id: str, message_id: str) -> ToolResult:
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return ToolResult(success=False, error="頻道不存在")
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.publish()
            return ToolResult(success=True, message="訊息已發布給追蹤者")
        except (discord.NotFound, ValueError):
            return ToolResult(success=False, error="訊息不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有發布權限（須為公告頻道）")


# ============================================================
# 身份組批次工具
# ============================================================


class AddRolesBulk(ToolBase):
    """一次將同一身份組指派給多位成員。"""

    name = "add_roles_bulk"
    description = "一次將同一個身份組指派給多位成員（批次操作）"
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "member_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "成員 ID 列表（純數字字串陣列）",
            },
            "role_id": {"type": "string", "description": "要指派的身份組 ID（純數字字串）"},
            "reason": {"type": "string", "description": "操作原因"},
        },
        "required": ["guild_id", "member_ids", "role_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, member_ids: list[str], role_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        role = _role(guild, role_id)
        if not role:
            return ToolResult(success=False, error="身份組不存在")
        ok, fail = 0, 0
        for mid in member_ids:
            member = _member(guild, mid)
            if not member:
                fail += 1
                continue
            try:
                await member.add_roles(role, reason=reason or None)
                ok += 1
            except discord.Forbidden:
                fail += 1
        return ToolResult(
            success=ok > 0,
            data={"assigned": ok, "failed": fail},
            message=f"已為 {ok} 位成員加上 @{role.name}，{fail} 位失敗",
        )


# ============================================================
# 貼圖（Sticker）工具
# ============================================================


class ListStickers(ToolBase):
    """列出伺服器貼圖。"""

    name = "list_stickers"
    description = "列出伺服器中所有自訂貼圖（Sticker）"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            stickers = await guild.fetch_stickers()
            data = [{"sticker_id": str(s.id), "name": s.name, "description": s.description} for s in stickers]
            return ToolResult(success=True, data={"stickers": data}, message=f"共 {len(data)} 個貼圖")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有讀取貼圖的權限")


class DeleteSticker(ToolBase):
    """刪除伺服器貼圖。"""

    name = "delete_sticker"
    description = "刪除伺服器中的自訂貼圖（不可逆）"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "sticker_id": {"type": "string", "description": "貼圖 ID（純數字字串）"},
            "reason": {"type": "string", "description": "刪除原因"},
        },
        "required": ["guild_id", "sticker_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, sticker_id: str, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        try:
            sticker = await guild.fetch_sticker(int(sticker_id))
            await sticker.delete(reason=reason or None)
            return ToolResult(success=True, message=f"貼圖「{sticker.name}」已刪除")
        except (discord.NotFound, ValueError):
            return ToolResult(success=False, error="貼圖不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有刪除貼圖的權限")


# ============================================================
# 成員管理進階工具
# ============================================================


class PruneMembersPreview(ToolBase):
    """預估可清理的閒置成員數量。"""

    name = "prune_members_preview"
    description = "預估有多少閒置成員可被清理（只預覽，不實際踢除），依據最近未上線天數"
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "days": {"type": "integer", "description": "閒置天數門檻（1-30）", "default": 30},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, days: int = 30) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        days = max(1, min(days, 30))
        try:
            count = await guild.estimate_pruned_members(days=days)
            return ToolResult(
                success=True,
                data={"estimated": count, "days": days},
                message=f"預估有 {count} 位成員閒置超過 {days} 天",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有檢視閒置成員的權限")


class PruneMembers(ToolBase):
    """清理閒置成員（踢除）。"""

    name = "prune_members"
    description = "清理（踢除）閒置超過指定天數的成員。不可逆，建議先用 prune_members_preview 預覽。"
    safety_level = SafetyLevel.DANGEROUS
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
            "days": {"type": "integer", "description": "閒置天數門檻（1-30）", "default": 30},
            "reason": {"type": "string", "description": "清理原因"},
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str, days: int = 30, reason: str = "") -> ToolResult:
        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="伺服器不存在")
        days = max(1, min(days, 30))
        try:
            pruned = await guild.prune_members(days=days, reason=reason or None)
            return ToolResult(
                success=True,
                data={"pruned": pruned, "days": days},
                message=f"已清理 {pruned} 位閒置成員",
            )
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有清理成員的權限")


# ============================================================
# 對話控制工具
# ============================================================


class SkipResponse(ToolBase):
    """主動選擇不回應這次的訊息。"""

    name = "skip_response"
    description = (
        "當你判斷這次的訊息「不需要你回應」時，呼叫這個工具來『保持沉默、不發任何訊息』。"
        "適用情境：訊息不是在跟你說話、只是別人之間的閒聊、你插話會很多餘、"
        "或內容跟你無關。請附上簡短的 reason 說明為什麼跳過。"
        "注意：呼叫此工具後就不會再發送任何訊息，請不要同時又想回覆。"
    )
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "跳過的原因，例如「不是在跟我說話」「只是閒聊」「Not interested」"},
        },
        "required": ["reason"],
    }

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, reason: str = "") -> ToolResult:
        return ToolResult(
            success=True,
            data={"skip": True, "reason": reason},
            message=f"已選擇不回應（{reason}）",
        )


class SwitchSubscriptionChannel(ToolBase):
    """切換 Agent 目前關注的訂閱頻道。"""

    name = "switch_subscription_channel"
    description = (
        "切換你在指定伺服器中的訂閱頻道。"
        "切換後，後續主要聊天上下文會改為新的頻道；其他頻道只保留摘要。"
        "適合在你判斷接下來應把注意力移到別的頻道或討論串時使用。"
    )
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串）"},
            "channel_id": {"type": "string", "description": "要切換成訂閱頻道的文字頻道或討論串 ID"},
            "channel_name": {"type": "string", "description": "頻道名稱（可選，若留空系統會自行解析）"},
        },
        "required": ["guild_id", "channel_id"],
    }

    def __init__(
        self,
        bot: discord.Client,
        subscription_switcher: Callable[[str, str, str], dict[str, str]] | None = None,
    ) -> None:
        self._bot = bot
        self._subscription_switcher = subscription_switcher

    async def execute(
        self,
        *,
        guild_id: str,
        channel_id: str,
        channel_name: str = "",
    ) -> ToolResult:
        if self._subscription_switcher is None:
            return ToolResult(success=False, error="訂閱頻道切換功能未啟用")

        guild = _guild(self._bot, guild_id)
        if guild is None:
            return ToolResult(success=False, error="伺服器不存在")

        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="目標不是可訂閱的文字頻道或討論串")

        actual_name = channel_name or getattr(channel, "name", "")
        switched = self._subscription_switcher(guild_id, channel_id, actual_name)
        return ToolResult(
            success=True,
            data={
                "previous_channel_id": switched.get("previous_channel_id", ""),
                "previous_channel_name": switched.get("previous_channel_name", ""),
                "channel_id": switched.get("channel_id", channel_id),
                "channel_name": switched.get("channel_name", actual_name),
            },
            message=f"已切換訂閱頻道到 {actual_name or channel_id}",
        )


# ============================================================
# 表情符號 / 視覺 工具
# ============================================================


class ListAvailableEmojis(ToolBase):
    """列出可在訊息中使用的表情符號（含 Unicode 常用 + 伺服器自訂）。"""

    name = "list_available_emojis"
    description = (
        "列出可在訊息中使用的表情符號清單，包含常用 Unicode emoji 與此伺服器的自訂表情。"
        "在訊息中使用自訂表情時，請用 `<:名稱:ID>` 格式（動態表情用 `<a:名稱:ID>`）；"
        "Unicode emoji 直接放入文字即可。"
    )
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串，如 1511547974143049808）"},
        },
        "required": ["guild_id"],
    }

    # 常用 Unicode emoji（給 AI 參考）
    COMMON_UNICODE = [
        "😀", "😂", "🤣", "😊", "😍", "😎", "🤔", "😅", "😭", "😡",
        "👍", "👎", "👌", "🙏", "👏", "🙌", "💪", "🫡", "🤝", "✌️",
        "❤️", "🔥", "✨", "🎉", "🎊", "💯", "⭐", "🌟", "💔", "💖",
        "✅", "❌", "⚠️", "❓", "❗", "💡", "📌", "📢", "🔔", "🚀",
        "🤖", "👀", "🥳", "😴", "🤡", "💀", "👻", "🙈", "🫠", "🗿",
    ]

    def __init__(self, bot: discord.Client) -> None:
        self._bot = bot

    async def execute(self, *, guild_id: str) -> ToolResult:
        guild = _guild(self._bot, guild_id)
        custom = []
        if guild:
            for e in guild.emojis:
                if not e.available:
                    continue
                tag = f"<a:{e.name}:{e.id}>" if e.animated else f"<:{e.name}:{e.id}>"
                custom.append({"name": e.name, "id": str(e.id), "animated": e.animated, "usage": tag})
        return ToolResult(
            success=True,
            data={
                "unicode_emojis": self.COMMON_UNICODE,
                "custom_emojis": custom,
                "custom_usage_format": "<:名稱:ID>（靜態） / <a:名稱:ID>（動態）",
            },
            message=f"Unicode {len(self.COMMON_UNICODE)} 個、自訂表情 {len(custom)} 個",
        )


class AnalyzeImage(ToolBase):
    """用視覺模型分析訊息中的圖片，並把結果存進資料庫。"""

    name = "analyze_image"
    description = (
        "用視覺模型解析指定訊息中的圖片內容，回傳文字描述並存入資料庫。"
        "適用於看不懂圖片時、需要了解使用者貼的圖在說什麼。"
        "若該圖片先前已分析過，會直接回傳快取結果。"
    )
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "頻道 ID（純數字字串）"},
            "message_id": {"type": "string", "description": "含圖片的訊息 ID（純數字字串）"},
            "prompt": {"type": "string", "description": "額外的分析指示（可選，例如「這張圖在玩什麼梗？」）"},
        },
        "required": ["channel_id", "message_id"],
    }

    def __init__(self, bot: discord.Client, ai_provider: Any = None, image_repo: Any = None, agent_name: str = "") -> None:
        self._bot = bot
        self._ai = ai_provider
        self._repo = image_repo
        self._agent_name = agent_name

    async def execute(self, *, channel_id: str, message_id: str, prompt: str = "") -> ToolResult:
        if self._ai is None:
            return ToolResult(success=False, error="視覺分析功能未啟用（缺少 AI provider）")
        channel = _channel(self._bot, channel_id)
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return ToolResult(success=False, error="頻道不存在或類型不正確")
        try:
            msg = await channel.fetch_message(int(message_id))
        except (discord.NotFound, ValueError):
            return ToolResult(success=False, error="訊息不存在")
        except discord.Forbidden:
            return ToolResult(success=False, error="沒有讀取訊息的權限")

        images = [
            a for a in msg.attachments
            if a.content_type and a.content_type.startswith("image/")
        ]
        # 也納入訊息 embed 內的圖片
        for emb in msg.embeds:
            if emb.image and emb.image.url:
                images.append(type("E", (), {"url": emb.image.url, "filename": "embed_image"})())  # type: ignore[arg-type]

        if not images:
            return ToolResult(success=False, error="該訊息沒有圖片")

        guild_id = str(msg.guild.id) if msg.guild else ""
        results = []
        for img in images:
            url = img.url
            filename = getattr(img, "filename", "")

            # 先查快取
            cached = None
            if self._repo is not None:
                cached = await self._repo.get_cached(message_id, url)
            if cached:
                results.append({"url": url, "filename": filename, "description": cached["description"], "cached": True})
                continue

            description = await self._ai.analyze_image(
                image_url=url, prompt=prompt, agent_name=self._agent_name
            )
            if not description:
                results.append({"url": url, "filename": filename, "description": "(無法解析或視覺功能停用)", "cached": False})
                continue

            # 存資料庫
            if self._repo is not None:
                await self._repo.insert(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    image_url=url,
                    description=description,
                    model=getattr(self._ai, "_config", None) and (self._ai._config.vision_model or self._ai._config.model) or "",
                    filename=filename,
                    agent_name=self._agent_name,
                )
            results.append({"url": url, "filename": filename, "description": description, "cached": False})

        return ToolResult(
            success=True,
            data={"images": results, "count": len(results)},
            message=f"已分析 {len(results)} 張圖片",
        )


# ============================================================
# 記憶工具
# ============================================================


class StoreMemory(ToolBase):
    """將一筆資訊寫入長期記憶，未來對話時可自動載入。"""

    name = "store_memory"
    description = (
        "將重要資訊寫入長期記憶，持久化保存。未來每次對話時，這些記憶會自動載入到你的 context 中。"
        "適用情境：記住使用者偏好、伺服器規則、重要決策、人物關係、頻道用途等。"
        "⚠️ 請勿濫用——只存真正重要、需要長期記住的資訊，不要存無意義的閒聊內容。"
        "分類指南：server_info=伺服器資訊, channel_purpose=頻道用途, rules=規則, "
        "user_preference=使用者偏好, decision=決策, agent_knowledge=Agent 自身知識。"
    )
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串）"},
            "category": {
                "type": "string",
                "description": "記憶分類：server_info / channel_purpose / rules / user_preference / decision / agent_knowledge",
            },
            "key": {
                "type": "string",
                "description": "記憶鍵（唯一標識，如使用者 ID、頻道 ID、rule_1 等）。同一 guild+category+key 會覆蓋舊值",
            },
            "value": {"type": "string", "description": "記憶內容（你要記住的東西）"},
            "confidence": {
                "type": "number",
                "description": "信心水準 0.0~1.0（預設 1.0）。不確定的資訊設低一點",
            },
        },
        "required": ["guild_id", "category", "key", "value"],
    }

    def __init__(self, bot: discord.Client, memory_service: Any = None) -> None:
        self._bot = bot
        self._memory = memory_service

    async def execute(
        self,
        *,
        guild_id: str,
        category: str,
        key: str,
        value: str,
        confidence: float = 1.0,
    ) -> ToolResult:
        if self._memory is None:
            return ToolResult(success=False, error="長期記憶服務未啟用")
        # 驗證 category
        valid_categories = {
            "server_info", "channel_purpose", "rules",
            "user_preference", "decision", "agent_knowledge",
        }
        if category not in valid_categories:
            return ToolResult(
                success=False,
                error=f"無效的分類 '{category}'，可用分類：{', '.join(sorted(valid_categories))}",
            )
        confidence = max(0.0, min(1.0, confidence))
        await self._memory.store(
            guild_id=guild_id,
            category=category,
            key=key,
            value=value,
            confidence=confidence,
        )
        return ToolResult(
            success=True,
            data={"guild_id": guild_id, "category": category, "key": key, "value": value, "confidence": confidence},
            message=f"已寫入長期記憶 [{category}] {key}: {value}",
        )


class RecallMemory(ToolBase):
    """查詢長期記憶——按分類或關鍵字搜尋。"""

    name = "recall_memory"
    description = (
        "查詢長期記憶。兩種用法：\n"
        "1. 指定 category：列出該分類下的所有記憶（如查所有 rules 或 user_preference）\n"
        "2. 指定 keyword：模糊搜尋 key 或 value 包含該關鍵字的記憶\n"
        "兩者可同時使用（在特定分類下搜尋關鍵字）。\n"
        "如果都不指定，則回傳整個伺服器的所有記憶（可能很多，建議加限制）。"
    )
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串）"},
            "category": {
                "type": "string",
                "description": "記憶分類篩選（可選）：server_info / channel_purpose / rules / user_preference / decision / agent_knowledge",
            },
            "keyword": {
                "type": "string",
                "description": "搜尋關鍵字（可選），模糊比對 key 或 value",
            },
            "limit": {
                "type": "integer",
                "description": "最多回傳筆數（預設 20）",
            },
        },
        "required": ["guild_id"],
    }

    def __init__(self, bot: discord.Client, memory_service: Any = None) -> None:
        self._bot = bot
        self._memory = memory_service

    async def execute(
        self,
        *,
        guild_id: str,
        category: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
    ) -> ToolResult:
        if self._memory is None:
            return ToolResult(success=False, error="長期記憶服務未啟用")
        if keyword:
            results = await self._memory.search_memory(
                guild_id=guild_id, keyword=keyword, category=category, limit=limit
            )
        else:
            results = await self._memory.retrieve(guild_id=guild_id, category=category)
            # 依 confidence 排序
            results.sort(key=lambda m: m.get("confidence", 1.0), reverse=True)
            results = results[:limit]
        # 格式化
        formatted = []
        for m in results:
            formatted.append({
                "category": m.get("category", ""),
                "key": m.get("key", ""),
                "value": m.get("value", ""),
                "confidence": m.get("confidence", 1.0),
                "updated_at": m.get("updated_at", ""),
            })
        return ToolResult(
            success=True,
            data={"memories": formatted, "count": len(formatted)},
            message=f"找到 {len(formatted)} 筆記憶" + (f"（搜尋: {keyword}）" if keyword else ""),
        )


class DeleteMemory(ToolBase):
    """刪除一筆長期記憶。"""

    name = "delete_memory"
    description = (
        "刪除一筆長期記憶。需要指定 guild_id、category、key 來精確定位要刪的記憶。"
        "⚠️ 這是刪除操作，刪了就沒了。請確認你真的要刪，不要手滑。"
    )
    safety_level = SafetyLevel.MODERATE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串）"},
            "category": {"type": "string", "description": "記憶分類"},
            "key": {"type": "string", "description": "記憶鍵（要刪除的記憶標識）"},
        },
        "required": ["guild_id", "category", "key"],
    }

    def __init__(self, bot: discord.Client, memory_service: Any = None) -> None:
        self._bot = bot
        self._memory = memory_service

    async def execute(
        self,
        *,
        guild_id: str,
        category: str,
        key: str,
    ) -> ToolResult:
        if self._memory is None:
            return ToolResult(success=False, error="長期記憶服務未啟用")
        deleted = await self._memory.delete_memory(
            guild_id=guild_id, category=category, key=key
        )
        if deleted:
            return ToolResult(
                success=True,
                data={"guild_id": guild_id, "category": category, "key": key},
                message=f"已刪除記憶 [{category}] {key}",
            )
        return ToolResult(
            success=False,
            error=f"找不到記憶 [{category}] {key}，可能已被刪除或從未存在",
        )


class StartCouncil(ToolBase):
    """發起 AI Council 討論。"""

    name = "start_council"
    description = (
        "發起 AI Council 討論，讓所有啟用中的 Agent 針對一個議題依序發言、必要時投票並產生結論。"
        "適用於高風險管理決策、Agent 意見可能分歧、需要多角度判斷，或使用者明確要求開會討論。"
        "請提供清楚的 topic，包含要討論的背景、目標與限制。"
    )
    safety_level = SafetyLevel.SAFE
    parameters_schema = {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "伺服器 ID（純數字字串）"},
            "channel_id": {"type": "string", "description": "目前頻道 ID（找不到 council 頻道時會 fallback 到這裡）"},
            "topic": {"type": "string", "description": "Council 討論主題，需包含背景、目標與限制"},
            "initiator": {"type": "string", "description": "發起者名稱（通常填你的 Agent 名字）"},
        },
        "required": ["guild_id", "channel_id", "topic", "initiator"],
    }

    def __init__(self, bot: discord.Client, council_provider: Any = None) -> None:
        self._bot = bot
        self._council_provider = council_provider

    def _get_council(self) -> Any:
        if callable(self._council_provider):
            return self._council_provider()
        return self._council_provider

    async def execute(
        self,
        *,
        guild_id: str,
        channel_id: str,
        topic: str,
        initiator: str,
    ) -> ToolResult:
        council = self._get_council()
        if council is None:
            return ToolResult(success=False, error="Council 尚未初始化或未啟用")
        if not getattr(council, "_config", None) or not council._config.enabled:
            return ToolResult(success=False, error="Council 功能未啟用")

        guild = _guild(self._bot, guild_id)
        if not guild:
            return ToolResult(success=False, error="找不到伺服器")

        state = council.get_state(guild_id)
        if getattr(state, "value", "") == "discussing" or getattr(state, "value", "") == "voting":
            return ToolResult(success=False, error="Council 已在討論中，請先等待目前討論結束")

        council_channel = discord.utils.get(
            guild.text_channels,
            name=getattr(council._config, "channel_name", "ai-council"),
        )
        fallback_channel = _channel(self._bot, channel_id)
        channel = council_channel if council_channel else fallback_channel
        if channel is not None and not isinstance(channel, discord.TextChannel):
            channel = None

        result = await council.start_discussion(
            guild_id=guild_id,
            topic=topic,
            initiator=initiator,
            channel=channel,
        )
        return ToolResult(
            success=True,
            data={
                "guild_id": guild_id,
                "topic": topic,
                "initiator": initiator,
                "channel_id": str(channel.id) if channel else "",
                "result": result,
            },
            message="Council 討論已完成",
        )


# ============================================================
# 工具集合工廠
# ============================================================


class DiscordToolCollection:
    """Discord 工具集合工廠。

    根據 Bot 實例自動建立所有 Discord 管理工具。
    """

    def __init__(
        self,
        bot: discord.Client,
        ai_provider: Any = None,
        image_repo: Any = None,
        agent_name: str = "",
        memory_service: Any = None,
        council_provider: Any = None,
        subscription_switcher: Callable[[str, str, str], dict[str, str]] | None = None,
    ) -> None:
        self._bot = bot
        self._tools: list[ToolBase] = [
            # SAFE — 讀取 / 低風險
            SendMessage(bot),
            ReplyMessage(bot),
            ReactionMessage(bot),
            ReadImage(bot),
            CreateThread(bot),
            ArchiveThread(bot),
            GetServerInfo(bot),
            GetChannelInfo(bot),
            GetMemberInfo(bot),
            SearchMessages(bot),
            GetPinnedMessages(bot),
            ListRoles(bot),
            ListMembers(bot),
            ListActiveThreads(bot),
            GetAuditLog(bot),
            CreateInvite(bot),
            ListInvites(bot),
            ListEmojis(bot),
            ListWebhooks(bot),
            GetVoiceParticipants(bot),
            ListScheduledEvents(bot),
            ListBans(bot),
            ListPermissions(bot),
            ListCategories(bot),
            ListAutoModRules(bot),
            GetChannelHistory(bot),
            GetMessage(bot),
            ListStickers(bot),
            PruneMembersPreview(bot),
            ListAvailableEmojis(bot),
            AnalyzeImage(bot, ai_provider=ai_provider, image_repo=image_repo, agent_name=agent_name),
            SwitchSubscriptionChannel(bot, subscription_switcher=subscription_switcher),
            SkipResponse(bot),
            # SAFE — 記憶
            StoreMemory(bot, memory_service=memory_service),
            RecallMemory(bot, memory_service=memory_service),
            StartCouncil(bot, council_provider=council_provider),
            # MODERATE — 可能影響他人
            EditMessage(bot),
            DeleteMessage(bot),
            BulkDeleteMessages(bot),
            PinMessage(bot),
            UnpinMessage(bot),
            SendDM(bot),
            CreateChannel(bot),
            CreateVoiceChannel(bot),
            CreateCategory(bot),
            EditChannel(bot),
            SetChannelSlowMode(bot),
            EditThread(bot),
            UnarchiveThread(bot),
            CreateRole(bot),
            EditRole(bot),
            EditRolePosition(bot),
            EditChannelPosition(bot),
            SetChannelPermissions(bot),
            CreateAutoModRule(bot),
            EditAutoModRule(bot),
            CreateStageChannel(bot),
            CreateForumChannel(bot),
            CreateForumPost(bot),
            CrosspostMessage(bot),
            AddRolesBulk(bot),
            AssignRole(bot),
            RemoveRole(bot),
            ChangeNickname(bot),
            MuteMember(bot),
            UnmuteMember(bot),
            TimeoutMember(bot),
            RemoveTimeout(bot),
            UnbanMember(bot),
            MoveMember(bot),
            DeleteInvite(bot),
            CreateWebhook(bot),
            CreateScheduledEvent(bot),
            CreateEmoji(bot),
            # MODERATE — 記憶（刪除）
            DeleteMemory(bot, memory_service=memory_service),
            # DANGEROUS — 不可逆 / 高風險
            DeleteChannel(bot),
            DeleteRole(bot),
            KickMember(bot),
            BanMember(bot),
            EditGuild(bot),
            DeleteEmoji(bot),
            DeleteScheduledEvent(bot),
            DeleteWebhook(bot),
            DeleteAutoModRule(bot),
            DeleteSticker(bot),
            PruneMembers(bot),
        ]

    @property
    def tools(self) -> list[ToolBase]:
        """取得所有工具。"""
        return self._tools

    @property
    def safe_tools(self) -> list[ToolBase]:
        """取得 SAFE 工具。"""
        return [t for t in self._tools if t.safety_level == SafetyLevel.SAFE]

    @property
    def moderate_tools(self) -> list[ToolBase]:
        """取得 MODERATE 工具。"""
        return [t for t in self._tools if t.safety_level == SafetyLevel.MODERATE]

    @property
    def dangerous_tools(self) -> list[ToolBase]:
        """取得 DANGEROUS 工具。"""
        return [t for t in self._tools if t.safety_level == SafetyLevel.DANGEROUS]

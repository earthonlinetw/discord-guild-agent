"""Discord 訊息內容解析器。

把一則 discord.Message 完整轉換為「人類/AI 可讀」的結構化文字與字典，
涵蓋：
- 純文字內容（content）
- 系統訊息類型（加入、Boost、置頂、開串等）
- Embed（標題、描述、欄位、footer 等）
- Components（按鈕、選單，含 Components V2：section / text_display / container...）
- 轉發訊息（message_snapshots，遞迴提取被轉發訊息的內容）
- 附件與貼圖

設計目標：讓 Agent 不只看到 message.content，還能理解整則訊息的全部資訊。
"""

from __future__ import annotations

from typing import Any

import discord


# ============================================================
# 系統訊息類型 → 中文說明
# ============================================================

_SYSTEM_TYPE_LABELS: dict[str, str] = {
    "recipient_add": "有人被加入群組",
    "recipient_remove": "有人離開群組",
    "call": "發起了通話",
    "channel_name_change": "變更了頻道名稱",
    "channel_icon_change": "變更了頻道圖示",
    "pins_add": "釘選了一則訊息",
    "new_member": "加入了伺服器",
    "premium_guild_subscription": "Boost 了這個伺服器 🚀",
    "premium_guild_tier_1": "Boost 讓伺服器升到了第 1 級 🚀",
    "premium_guild_tier_2": "Boost 讓伺服器升到了第 2 級 🚀",
    "premium_guild_tier_3": "Boost 讓伺服器升到了第 3 級 🚀",
    "channel_follow_add": "追蹤了一個公告頻道",
    "guild_stream": "開始了直播",
    "thread_created": "建立了一個討論串",
    "reply": "回覆了一則訊息",
    "chat_input_command": "使用了斜線指令",
    "thread_starter_message": "討論串的起始訊息",
    "guild_invite_reminder": "邀請提醒",
    "context_menu_command": "使用了情境選單指令",
    "auto_moderation_action": "自動審核採取了行動",
    "stage_start": "開始了舞台活動",
    "stage_end": "結束了舞台活動",
    "stage_speaker": "成為舞台發言者",
    "stage_topic": "變更了舞台主題",
}


def message_type_label(msg: discord.Message) -> str:
    """取得訊息類型的中文說明（default / reply 不算特殊系統訊息）。"""
    type_name = getattr(msg.type, "name", str(msg.type))
    if type_name in ("default", "reply"):
        return ""
    return _SYSTEM_TYPE_LABELS.get(type_name, f"系統訊息（{type_name}）")


def channel_lineage(channel: Any) -> dict[str, str]:
    """提取頻道 / 討論串的層級資訊。"""
    parent = getattr(channel, "parent", None)
    category = getattr(channel, "category", None)
    if category is None and parent is not None:
        category = getattr(parent, "category", None)

    return {
        "channel_name": getattr(channel, "name", "") or "",
        "channel_type": getattr(getattr(channel, "type", None), "name", str(getattr(channel, "type", ""))) or "",
        "category_name": getattr(category, "name", "") or "",
        "parent_channel_id": str(getattr(parent, "id", "") or ""),
        "parent_channel_name": getattr(parent, "name", "") or "",
    }


def reaction_counts(msg: discord.Message) -> dict[str, int]:
    """提取訊息目前的反應數量快照。"""
    counts: dict[str, int] = {}
    for reaction in msg.reactions:
        counts[str(reaction.emoji)] = reaction.count
    return counts


# ============================================================
# Embed 提取
# ============================================================


def extract_embed(embed: discord.Embed) -> dict[str, Any]:
    """把 Embed 轉成精簡字典。"""
    data: dict[str, Any] = {}
    if embed.title:
        data["title"] = embed.title
    if embed.description:
        data["description"] = embed.description
    if embed.url:
        data["url"] = embed.url
    if embed.author and embed.author.name:
        data["author"] = embed.author.name
    if embed.footer and embed.footer.text:
        data["footer"] = embed.footer.text
    if embed.image and embed.image.url:
        data["image"] = embed.image.url
    if embed.thumbnail and embed.thumbnail.url:
        data["thumbnail"] = embed.thumbnail.url
    if embed.fields:
        data["fields"] = [
            {"name": f.name, "value": f.value, "inline": f.inline}
            for f in embed.fields
        ]
    return data


def embed_to_text(embed: discord.Embed) -> str:
    """把 Embed 轉成可讀文字。"""
    parts: list[str] = []
    if embed.author and embed.author.name:
        parts.append(f"〔{embed.author.name}〕")
    if embed.title:
        parts.append(f"**{embed.title}**")
    if embed.description:
        parts.append(embed.description)
    for f in embed.fields:
        parts.append(f"• {f.name}: {f.value}")
    if embed.footer and embed.footer.text:
        parts.append(f"— {embed.footer.text}")
    return "\n".join(parts)


# ============================================================
# Components（含 V2）提取
# ============================================================


def extract_component(component: Any) -> dict[str, Any]:
    """遞迴提取單一 component 的資訊。"""
    ctype = getattr(component, "type", None)
    type_name = getattr(ctype, "name", str(ctype)) if ctype is not None else "unknown"
    info: dict[str, Any] = {"type": type_name}

    # 按鈕
    label = getattr(component, "label", None)
    if label:
        info["label"] = label
    url = getattr(component, "url", None)
    if url:
        info["url"] = url
    emoji = getattr(component, "emoji", None)
    if emoji:
        info["emoji"] = str(emoji)

    # 選單 placeholder / 選項
    placeholder = getattr(component, "placeholder", None)
    if placeholder:
        info["placeholder"] = placeholder
    options = getattr(component, "options", None)
    if options:
        info["options"] = [getattr(o, "label", str(o)) for o in options]

    # Components V2：text_display 的內容
    content = getattr(component, "content", None)
    if content and type_name == "text_display":
        info["text"] = content

    # 巢狀子元件（action_row / section / container 等）
    children = getattr(component, "children", None) or getattr(component, "components", None)
    if children:
        try:
            info["children"] = [extract_component(c) for c in children]
        except TypeError:
            pass

    return info


def components_to_text(components: list[Any]) -> str:
    """把 components 轉成可讀文字（盡量描述按鈕/文字顯示）。"""
    lines: list[str] = []

    def walk(comp: Any, depth: int = 0) -> None:
        ctype = getattr(comp, "type", None)
        type_name = getattr(ctype, "name", str(ctype)) if ctype is not None else "unknown"
        prefix = "  " * depth

        if type_name == "button":
            label = getattr(comp, "label", "") or getattr(comp, "emoji", "")
            url = getattr(comp, "url", None)
            lines.append(f"{prefix}[按鈕] {label}" + (f" → {url}" if url else ""))
        elif type_name == "text_display":
            txt = getattr(comp, "content", "")
            if txt:
                lines.append(f"{prefix}{txt}")
        elif type_name in ("select", "user_select", "role_select", "channel_select", "mentionable_select"):
            ph = getattr(comp, "placeholder", "") or "選單"
            lines.append(f"{prefix}[選單] {ph}")

        children = getattr(comp, "children", None) or getattr(comp, "components", None)
        if children:
            try:
                for c in children:
                    walk(c, depth + 1)
            except TypeError:
                pass

    for comp in components:
        walk(comp)
    return "\n".join(lines)


# ============================================================
# 完整訊息序列化
# ============================================================


def serialize_message(msg: discord.Message, *, include_snapshots: bool = True) -> dict[str, Any]:
    """把一則訊息完整序列化為字典。"""
    data: dict[str, Any] = {
        "message_id": str(msg.id),
        "author": msg.author.display_name,
        "author_id": str(msg.author.id),
        "content": msg.content,
        "type": getattr(msg.type, "name", str(msg.type)),
        "timestamp": msg.created_at.isoformat(),
    }

    sys_label = message_type_label(msg)
    if sys_label:
        data["system_event"] = sys_label

    # Embeds
    if msg.embeds:
        data["embeds"] = [extract_embed(e) for e in msg.embeds]

    # Components（含 V2）
    if msg.components:
        data["components"] = [extract_component(c) for c in msg.components]

    # 附件
    if msg.attachments:
        data["attachments"] = [
            {"filename": a.filename, "url": a.url, "content_type": a.content_type}
            for a in msg.attachments
        ]

    # 貼圖
    if msg.stickers:
        data["stickers"] = [s.name for s in msg.stickers]

    # 轉發訊息（message snapshots，例如 Forward 功能）
    if include_snapshots and getattr(msg, "message_snapshots", None):
        snapshots = []
        for snap in msg.message_snapshots:
            snap_data: dict[str, Any] = {
                "content": getattr(snap, "content", ""),
                "type": getattr(getattr(snap, "type", None), "name", ""),
            }
            snap_embeds = getattr(snap, "embeds", None)
            if snap_embeds:
                snap_data["embeds"] = [extract_embed(e) for e in snap_embeds]
            snap_attachments = getattr(snap, "attachments", None)
            if snap_attachments:
                snap_data["attachments"] = [
                    {"filename": a.filename, "url": a.url} for a in snap_attachments
                ]
            snap_components = getattr(snap, "components", None)
            if snap_components:
                snap_data["components"] = [extract_component(c) for c in snap_components]
            snapshots.append(snap_data)
        data["forwarded_messages"] = snapshots

    # Interaction Metadata（此訊息是來自 interaction 的回應）
    imeta = getattr(msg, "interaction_metadata", None)
    if imeta is not None:
        interaction_data: dict[str, Any] = {
            "id": str(imeta.id),
            "type": getattr(imeta.type, "name", str(imeta.type)),
            "user": imeta.user.display_name,
            "user_id": str(imeta.user.id),
        }
        # 判斷 User Install vs Guild Install
        is_guild = imeta.is_guild_integration()
        is_user = imeta.is_user_integration()
        interaction_data["is_guild_integration"] = is_guild
        interaction_data["is_user_integration"] = is_user
        if not is_guild and is_user:
            interaction_data["install_type"] = "user_install"
        elif is_guild:
            interaction_data["install_type"] = "guild_install"
        # 追加資訊
        if imeta.original_response_message_id:
            interaction_data["original_response_message_id"] = str(imeta.original_response_message_id)
        if imeta.interacted_message_id:
            interaction_data["interacted_message_id"] = str(imeta.interacted_message_id)
        if imeta.target_user:
            interaction_data["target_user"] = imeta.target_user.display_name
            interaction_data["target_user_id"] = str(imeta.target_user.id)
        if imeta.target_message_id:
            interaction_data["target_message_id"] = str(imeta.target_message_id)
        data["interaction_metadata"] = interaction_data

    return data


def message_to_readable_text(msg: discord.Message) -> str:
    """把一則訊息轉成「AI 友善」的可讀文字。

    用於收集訊息時，讓 Agent 不只看到 content，還能理解 embed、按鈕、
    轉發內容、系統事件等。回傳的文字會盡量精簡但完整。
    """
    parts: list[str] = []

    # 系統事件（加入 / Boost / 釘選等）
    sys_label = message_type_label(msg)
    if sys_label:
        parts.append(f"[系統事件] {msg.author.display_name} {sys_label}")

    # 純文字
    if msg.content:
        parts.append(msg.content)

    # Embeds
    for embed in msg.embeds:
        text = embed_to_text(embed)
        if text:
            parts.append(f"[嵌入內容]\n{text}")

    # Components（按鈕 / 選單 / V2 文字）
    if msg.components:
        ctext = components_to_text(msg.components)
        if ctext:
            parts.append(f"[互動元件]\n{ctext}")

    # 附件
    if msg.attachments:
        names = ", ".join(a.filename for a in msg.attachments)
        parts.append(f"[附件] {names}")

    # 貼圖
    if msg.stickers:
        parts.append(f"[貼圖] {', '.join(s.name for s in msg.stickers)}")

    # 轉發訊息
    snapshots = getattr(msg, "message_snapshots", None)
    if snapshots:
        for snap in snapshots:
            snap_parts: list[str] = []
            snap_content = getattr(snap, "content", "")
            if snap_content:
                snap_parts.append(snap_content)
            for embed in getattr(snap, "embeds", []) or []:
                t = embed_to_text(embed)
                if t:
                    snap_parts.append(t)
            snap_attachments = getattr(snap, "attachments", None)
            if snap_attachments:
                snap_parts.append("附件: " + ", ".join(a.filename for a in snap_attachments))
            if snap_parts:
                parts.append("[轉發訊息]\n" + "\n".join(snap_parts))

    # Interaction Metadata
    imeta = getattr(msg, "interaction_metadata", None)
    if imeta is not None:
        itype = getattr(imeta.type, "name", str(imeta.type))
        is_guild = imeta.is_guild_integration()
        is_user = imeta.is_user_integration()
        is_user_install = not is_guild and is_user

        if is_user_install:
            parts.append(
                f"[User Install 互動] {imeta.user.display_name} 透過 User Install "
                f"使用了 {itype}（此人是 interaction 發起人）"
            )
        elif is_guild:
            parts.append(
                f"[Guild Install 互動] {imeta.user.display_name} "
                f"使用了 {itype}"
            )
        else:
            parts.append(
                f"[互動] {imeta.user.display_name} "
                f"使用了 {itype}"
            )

        if imeta.target_user:
            parts.append(f"  → 對象：{imeta.target_user.display_name}")
        if imeta.target_message_id:
            parts.append(f"  → 目標訊息 ID：{imeta.target_message_id}")

    return "\n".join(parts).strip()

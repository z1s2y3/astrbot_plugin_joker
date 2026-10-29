"""Canonical QQ group-management feature catalogue.

This module intentionally has no AstrBot imports so both the bot and the
standalone customer portal can use the same primary-category metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re


# Keep command aliases in the JSON/string projection so old Windows clients
# remain compatible.  The parser accepts the separators commonly entered in
# the Chinese web form, while always returning one canonical list.
COMMAND_ALIAS_SEPARATOR_RE = re.compile(r"[|,，、;；]+")
MAX_COMMAND_ALIASES = 8
MAX_COMMAND_ALIAS_LENGTH = 32


def split_command_aliases(value: object, default: str) -> tuple[str, ...]:
    """Return safe, de-duplicated command tokens in configured order.

    A blank or malformed value falls back to ``default``.  A value such as
    ``"签到|打卡，今日签到"`` therefore becomes three runtime aliases, while
    persistence can continue storing the original compact string.
    """

    fallback = str(default or "").strip().lstrip("/")
    if not fallback or any(ch.isspace() for ch in fallback):
        fallback = "指令"

    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    tokens: list[str] = []
    for item in COMMAND_ALIAS_SEPARATOR_RE.split(raw):
        token = item.strip().lstrip("/")
        if (
            not token
            or len(token) > MAX_COMMAND_ALIAS_LENGTH
            or any(ch.isspace() for ch in token)
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in token)
        ):
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= MAX_COMMAND_ALIASES:
            break
    if not tokens:
        return (fallback,)
    return tuple(tokens)


def primary_command_alias(value: object, default: str) -> str:
    """Return the first alias for help text and backwards-compatible fields."""

    return split_command_aliases(value, default)[0]


def format_command_aliases(value: object, default: str) -> str:
    """Return the canonical separator form used by portal projections."""

    return "|".join(split_command_aliases(value, default))


PERMISSION_LABELS = {
    "member": "所有成员",
    "admin": "群管理员/群主",
    "owner": "群主/机器人主人",
    "owner_whitelist": "机器人主人/白名单群",
    "sub_admin": "分群管理员/群管理员",
}


def permission_label(value: object, default: str = "member") -> str:
    """Return the stable user-facing label for a command permission."""

    key = str(value or default).strip().lower()
    return PERMISSION_LABELS.get(key, PERMISSION_LABELS.get(default, "所有成员"))


@dataclass(frozen=True)
class FeatureCategory:
    key: str
    label: str
    enabled_key: str
    aliases: tuple[str, ...]
    help_handler: str
    summary: str


FEATURE_CATEGORIES: tuple[FeatureCategory, ...] = (
    FeatureCategory("audit", "入群审核", "audit_enabled", ("审核",), "_show_audit_help", "验证、审批、白名单与审核日志"),
    FeatureCategory("blacklist", "黑名单系统", "blacklist_enabled", ("黑名单",), "_show_blacklist_help", "分群/全局黑名单及自动处理"),
    FeatureCategory("mute", "禁言系统", "mute_enabled", ("禁言",), "_show_mute_help", "成员禁言、全体禁言与解禁提示"),
    FeatureCategory("kick", "踢出系统", "kick_enabled", ("踢出",), "_show_kick_help", "踢出、连带踢出与操作记录"),
    FeatureCategory("recall", "撤回系统", "recall_enabled", (), "_show_recall_help", "手动/自动撤回与群消息记录"),
    FeatureCategory("welcome", "入群欢迎", "welcome_enabled", ("欢迎系统", "欢迎"), "_show_welcome_help", "欢迎、退群提示、艾特与延迟"),
    FeatureCategory("reply", "自动回复", "reply_enabled", ("自定义回复",), "_show_reply_help", "精确/模糊回复和触发冷却"),
    FeatureCategory("lottery", "抽奖", "lottery_enabled", ("群抽奖",), "_show_lottery_help", "每群独立奖项、概率、库存与抽取记录"),
    FeatureCategory("stats", "统计系统", "stats_enabled", (), "_show_stats_help", "签到、积分与邀请统计"),
    FeatureCategory("signin", "签到", "stats_enabled", ("每日签到",), "_show_signin_help", "每日签到并领取连续签到积分"),
    FeatureCategory("signin_status", "签到状态", "stats_enabled", ("我的签到",), "_show_signin_status_help", "查看今日、连续及累计签到"),
    FeatureCategory("signin_calendar", "签到日历", "stats_enabled", ("本月签到", "签到记录"), "_show_signin_calendar_help", "查看本月签到日期"),
    FeatureCategory("signin_ranking", "签到排行", "stats_enabled", ("签到排行榜", "连签排行"), "_show_signin_ranking_help", "按累计签到天数排行"),
    FeatureCategory("ranking", "排行榜", "stats_enabled", ("积分排行",), "_show_ranking_help", "查看群积分排行"),
    FeatureCategory("user_info", "我的信息", "stats_enabled", ("个人信息",), "_show_user_info_help", "查看自己的积分和签到资料"),
    FeatureCategory("group_stats", "群统计", "stats_enabled", ("群数据",), "_show_group_stats_help", "查看群签到和积分汇总"),
    FeatureCategory("timed_message", "定时消息", "timed_message_enabled", (), "_show_timed_msg_help", "每日播报及 Cron 定时任务"),
    FeatureCategory("sub_admin", "分群管理员", "sub_admin_enabled", (), "_show_sub_admin_help", "向指定成员授予分群管理权限"),
    FeatureCategory("text_to_image", "文转图", "text_to_image_enabled", (), "_show_text_to_image_help", "机器人文字与图片输出切换"),
    FeatureCategory("group_config", "群设置", "group_config_enabled", ("分群设置",), "_show_group_config_help", "查看和维护本群独立配置"),
    FeatureCategory("keyword_filter", "关键词过滤", "keyword_filter_enabled", ("过滤",), "_show_keyword_filter_help", "关键词/正则过滤及违规动作"),
    FeatureCategory("player_reply", "用户专属回复", "player_reply_enabled", ("专属回复",), "_show_player_reply_help", "为指定成员设置专属随机回复"),
    FeatureCategory("scum", "SCUM 查询", "scum_enabled", ("服务器查询",), "show_scum_help", "服务器关键词、在线状态和地址查询命令"),
    FeatureCategory("game_commands", "游戏指令", "game_commands_enabled", (), "_show_game_commands_help", "绑定 SteamID 后执行固定 SCUM 游戏指令"),
)


_STATS_ACTION_KEYS = {
    "signin",
    "signin_status",
    "signin_calendar",
    "signin_ranking",
    "ranking",
    "user_info",
    "group_stats",
}

PRIMARY_CONFIGURABLE_FEATURES: tuple[FeatureCategory, ...] = tuple(
    category for category in FEATURE_CATEGORIES if category.key not in _STATS_ACTION_KEYS
)

# Message history is configured as part of the recall system rather than
# exposed as a 22nd primary menu category, but it must remain available to the
# common feature switch/settings commands.
AUXILIARY_FEATURES: tuple[FeatureCategory, ...] = (
    FeatureCategory(
        "message_cache",
        "群消息记录",
        "message_cache_enabled",
        ("消息记录",),
        "_show_recall_help",
        "保存文本、图片、表情等消息段供撤回和审计",
    ),
    # Lightweight chat games are kept auxiliary so the established primary
    # management menu remains byte-for-byte compatible for older clients.
    FeatureCategory(
        "entertainment",
        "娱乐",
        "entertainment_enabled",
        ("娱乐系统", "小游戏", "娱乐帮助"),
        "_show_entertainment_help",
        "骰子、猜拳和随机选择等轻量互动",
    ),
    FeatureCategory(
        "game_servers",
        "游戏服务器",
        "game_servers_enabled",
        (),
        "_show_game_commands_help",
        "配置本群使用的多个 SCUM 插件 WebSocket 服务器",
    ),
)
CONFIGURABLE_FEATURES = PRIMARY_CONFIGURABLE_FEATURES + AUXILIARY_FEATURES
FEATURE_BY_KEY = {
    category.key: category
    for category in (*FEATURE_CATEGORIES, *AUXILIARY_FEATURES)
}
FEATURE_BY_COMMAND = {
    command: category
    for category in (*FEATURE_CATEGORIES, *AUXILIARY_FEATURES)
    for command in (category.label, *category.aliases)
}


@lru_cache(maxsize=1)
def feature_alias_map() -> dict[str, str]:
    """Return every accepted key/Chinese name mapped to its enabled key."""
    return {
        name: category.enabled_key
        for category in CONFIGURABLE_FEATURES
        for name in (category.key, category.label, *category.aliases)
    }

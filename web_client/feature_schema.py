"""Allow-listed group settings exposed by the customer portal.

Keeping this list explicit prevents a crafted form request from overwriting
runtime records such as sign-in history, audit queues, or message caches.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

try:
    from feature_catalog import (
        FEATURE_BY_KEY as GROUP_FEATURE_BY_KEY,
        split_command_aliases,
    )
except ImportError:  # pragma: no cover - package imported through its parent
    from ..feature_catalog import (
        FEATURE_BY_KEY as GROUP_FEATURE_BY_KEY,
        split_command_aliases,
    )


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: str
    default: Any
    help: str = ""
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[tuple[str, str], ...] = ()
    max_length: int = 2000
    group_only: bool = False


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    label: str
    description: str
    fields: tuple[FieldSpec, ...]


TEMPLATE_VARIABLE_HELP = (
    "变量说明：{user_name}/{member_name}=群名片或昵称；{user_id}/{qq}=QQ号；"
    "{group_name}=群名称；{group_id}=群号；{keyword}=命中的关键词；"
    "{reason}=处理原因；{duration}=禁言秒数。未知变量会原样保留。"
)


def b(
    key: str,
    label: str,
    default: bool = False,
    help: str = "",
    *,
    group_only: bool = False,
) -> FieldSpec:
    return FieldSpec(key, label, "bool", default, help, group_only=group_only)


def i(
    key: str,
    label: str,
    default: int,
    minimum: int,
    maximum: int,
    help: str = "",
) -> FieldSpec:
    return FieldSpec(key, label, "int", default, help, minimum, maximum)


def s(
    key: str,
    label: str,
    default: str = "",
    help: str = "",
    max_length: int = 500,
) -> FieldSpec:
    return FieldSpec(key, label, "string", default, help, max_length=max_length)


def text(key: str, label: str, default: str = "", help: str = "") -> FieldSpec:
    return FieldSpec(key, label, "text", default, help, max_length=4000)


def items(
    key: str,
    label: str,
    default: list[Any],
    help: str = "",
) -> FieldSpec:
    return FieldSpec(key, label, "list", default, help, max_length=4000)


def choice(
    key: str,
    label: str,
    default: str,
    choices: tuple[tuple[str, str], ...],
    help: str = "",
    *,
    group_only: bool = False,
) -> FieldSpec:
    return FieldSpec(
        key,
        label,
        "choice",
        default,
        help,
        choices=choices,
        group_only=group_only,
    )


def multi_choice(
    key: str,
    label: str,
    default: list[str],
    choices: tuple[tuple[str, str], ...],
    help: str = "",
    *,
    group_only: bool = False,
) -> FieldSpec:
    return FieldSpec(
        key,
        label,
        "multichoice",
        default,
        help,
        choices=choices,
        group_only=group_only,
    )


def default_steam_signin_profiles() -> list[dict[str, Any]]:
    """Return a disabled starter profile for the Steam sign-in editor."""

    return [
        {
            "id": "steam-daily-example",
            "name": "Steam 每日签到示例（修改后启用）",
            "command": "签到游戏",
            "mode": "steam",
            "schedule": "daily",
            "enabled": False,
            "base_reward": 10,
            "reward_3_days": 30,
            "reward_7_days": 100,
            "reward_14_days": 200,
            "reward_30_days": 500,
            "steam_reward_mode": "database",
            "steam_reward_operation": "execute_game_command",
            "steam_reward_command": "#ChangeCurrencyBalance Normal {reward} {steam_id}",
            "steam_reward_failure_message": "签到奖励执行失败，未完成本次签到，请稍后重试。",
            "signin_message": "{name} 的 Steam 签到成功，第 {count} 次，奖励 {reward}。",
        }
    ]


def default_database_profiles() -> list[dict[str, Any]]:
    """New groups obtain database descriptors only from FTP server profiles."""

    return []


FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        "audit",
        "入群审核",
        "为新成员和成员邀请启用数学验证、ID 验证、管理员审批或直接审批。审核规则由 Windows 机器人执行，网页只保存本群设置。",
        (
            b("enabled", "启用入群审核", False, "关闭后不创建新的审核或验证请求；已存在的待处理请求仍由 Windows 机器人按其当前状态处理。"),
            choice(
                "approval_mode",
                "审核方式",
                "math",
                (
                    ("id", "ID 验证"),
                    ("code", "管理员审批"),
                    ("math", "数学验证"),
                    ("direct", "直接审批"),
                    ("steamid", "SteamID 查库"),
                ),
                "ID 验证会让新成员按机器人给出的 ID 完成验证；管理员审批会生成审批码；数学验证会让新成员回答算式；直接审批会在收到申请后自动同意；SteamID 查库会先验证当前服务器数据库中存在该 SteamID，成功后才批准入群并设置游戏名片。",
            ),
            i("verification_timeout", "验证或审批超时（秒）", 300, 30, 3600, "超过此时间未完成的请求会由 Windows 审核流程处理为超时。"),
            text("verification_timeout_message", "验证超时提示", "用户 {user_name}({user_id}) 验证超时，{reason}。", TEMPLATE_VARIABLE_HELP),
            i("max_attempts", "数学验证最大尝试次数", 3, 1, 20, "仅数学验证使用；达到次数后，机器人按审核规则移出未通过成员。"),
            b("kick_on_verification_failure", "验证失败后踢出成员", False, "达到最大失败次数或验证超时后是否移出成员；默认关闭，只清理审核记录并保留成员。"),
            b("admin_bypass", "管理员/群主邀请免验证", True, "开启后，由群主或管理员邀请入群的成员不会进入邀请验证。"),
            b("enable_invite_audit", "审核成员邀请", True, "关闭后只审核普通入群申请，不为成员邀请创建验证请求。"),
            text("steamid_success_message", "SteamID 验证欢迎语", "SteamID 验证成功，欢迎 {user_name} 加入本群！", TEMPLATE_VARIABLE_HELP),
            text("approval_success_message", "审核通过回复", "已批准用户 {user_name}({user_id}) 入群。", TEMPLATE_VARIABLE_HELP),
            text("approval_failure_message", "审核失败回复", "审核失败：{reason}", TEMPLATE_VARIABLE_HELP),
            text("rejection_success_message", "拒绝入群回复", "已拒绝用户 {user_name}({user_id}) 的入群申请。理由：{reason}", TEMPLATE_VARIABLE_HELP),
        ),
    ),
    FeatureSpec(
        "welcome",
        "入群欢迎",
        "设置欢迎、退群提示及欢迎触发方式。",
        (
            b("enabled", "启用欢迎功能"),
            b("auto_welcome", "自动欢迎", False),
            i("delay", "发送延迟（秒）", 0, 0, 3600, "成员入群后等待多久再发送；0 表示立即发送。"),
            text("message", "欢迎语", "欢迎 {user_name} 加入本群！", "自动欢迎使用这段文字。可用变量：{user_name}、{user_id}、{group_id}；若启用 @ 新成员，会在文字前单独 @ 对方。"),
            b("at_new_member", "@ 新成员", True),
            b("farewell_enabled", "启用退群提示"),
            text("farewell_message", "退群提示", "{user_name} 离开了本群", TEMPLATE_VARIABLE_HELP),
            b("auto_reply_enabled", "启用欢迎关键词回复", True, "有人发送下方任一关键词时，机器人回复欢迎语；这与成员入群自动欢迎是两个独立开关。填入关键词后默认生效，可单独关闭。"),
            items("keywords", "欢迎关键词", ["欢迎", "welcome", "入群"], "每行一个关键词。仅启用“欢迎关键词回复”时生效。"),
        ),
    ),
    FeatureSpec(
        "blacklist",
        "黑名单",
        "管理黑名单成员加入本群、主动退群和验证失败后的自动处理。黑名单记录在 Windows 本地保存，可在“黑名单记录”查看。",
        (
            b("enabled", "启用黑名单"),
            b("auto_kick", "自动踢出黑名单成员", True, "仅处理进入本群后命中的黑名单成员；每日自动踢出上限仍然生效。"),
            b("auto_blacklist_on_leave", "主动退群自动拉黑", False, "仅主动退群会被记录；被踢出成员不会因为这一项再次加入黑名单。"),
            b("auto_blacklist_on_verify_fail", "验证失败自动拉黑", False, "入群审核达到失败次数后，将该成员加入本群黑名单。"),
        ),
    ),
    FeatureSpec(
        "mute",
        "禁言系统",
        "配置默认禁言等级和时长。",
        (
            b("enabled", "启用禁言系统"),
            i("level", "默认违规等级", 5, 1, 10),
            i("duration", "默认禁言时长（秒）", 300, 1, 2_592_000),
        ),
    ),
    FeatureSpec(
        "kick",
        "踢出系统",
        "记录本群踢出次数，并可在达到阈值后把该成员加入黑名单。踢出记录属于 Windows 运行数据，不会被网页覆盖。",
        (
            b("enabled", "启用踢出系统"),
            b("auto_blacklist", "达到阈值后自动拉黑", False, "开启后，同一成员累计踢出次数达到下方阈值时，机器人将其加入本群黑名单。"),
            i("limit", "自动拉黑踢出阈值", 3, 1, 100, "按同一成员的累计踢出记录计算；不是每天的总踢出次数。"),
        ),
    ),
    FeatureSpec(
        "recall",
        "撤回与违规检测",
        "按关键词、正则及消息文本、名片、转发、网址内容执行分类检测。命中后先撤回，再按严重违规策略警告、禁言、踢出或拉黑。",
        (
            b("enabled", "启用撤回系统", False, "关闭后不再自动撤回新消息；不会删除已经保存的撤回关键词或记录。"),
            choice(
                "mode",
                "撤回模式",
                "keyword",
                (("keyword", "关键词"), ("regex", "正则"), ("all", "全部消息"), ("none", "不自动撤回")),
            ),
            i("time_limit", "可撤回时间（秒）", 60, 1, 3600, "消息超过平台可撤回时间后，QQ 可能拒绝撤回；此值也用于机器人自身的延迟撤回。"),
            items("keywords", "撤回关键词", [], "关键词模式下每行一个；成员消息包含任一关键词即撤回。可在群内使用“/查看撤回关键词”查看当前列表。"),
            items("regex_patterns", "撤回正则", [], "正则模式下每行一个 Python 正则，例如 (?i)广告|推广；不要写前后 /。正则只用于匹配文字，不会匹配图片内容。"),
            b("self_recall", "机器人自身消息自动撤回", True, "控制机器人自己发出的回复是否在撤回时间后自动撤回；关闭后已排队的自身撤回也会停止。"),
            b("notification", "撤回后发送提示", False, "仅对自动撤回命中的成员消息发送提示，不影响机器人自身撤回。"),
            text("notification_message", "撤回提示", "您的消息包含违规内容，已被撤回", TEMPLATE_VARIABLE_HELP),
            text("manual_failure_message", "手动撤回失败回复", "撤回失败：{reason}", "手动批量撤回部分失败时显示；平台拒绝某条消息不会阻止其他消息继续处理。" + TEMPLATE_VARIABLE_HELP),
            b("enable_admin_recall", "允许管理员撤回成员消息", True, "控制群管理员使用撤回指令时是否可以处理成员消息。"),
            b("enable_sensitive_detection", "自动检测违规内容", True, "检测黄色、暴力、政治、诈骗赌博、违规网址及消息卡片/转发中的文字；仅在撤回系统开启时执行。"),
            b("kick_on_severe_violation", "严重违规自动踢出", True, "命中黄色、暴力或违规卡片等高风险内容时，撤回后自动踢出发言人；关闭时只撤回并按提示处理。"),
            items("sensitive_words", "自定义敏感词", ["色情", "黄图", "黄片", "裸聊", "裸照", "成人视频"], "每行一个自定义敏感词；会和内置黄色、暴力、政治、诈骗赌博词库一起检测。"),
        ),
    ),
    FeatureSpec(
        "message_cache",
        "群消息记录",
        "在 Windows 本地保存本群的文本、图片、表情和撤回标记，供撤回、审计及网页“消息收发”查看。运行记录不会随网页保存被重置。",
        (
            b("enabled", "启用群消息记录", True, "关闭后不再写入新的本地记录，已有记录按保留期继续清理。"),
            b("cache_raw_message", "保存原始消息内容", True, "关闭后仍会保留必要的时间、方向和操作摘要，但不会保存完整原始消息文本。"),
            i("retention_days", "保留天数", 7, 0, 3650, "0 表示不自动清理；该设置同时作用于文字记录和已归档的群图片。"),
            i("max_messages_per_user", "单个成员最多记录数", 500, 1, 5000, "达到上限时保留较新的记录，避免单个成员占满本群缓存。"),
            b("archive_images", "归档群图片", True, "将群消息图片保存到 Windows 本地归档，避免 QQ 临时地址失效；网页只显示已授权的消息摘要。"),
            i("archive_image_max_mb", "归档单张图片上限（MB）", 20, 1, 100, "只用于群消息归档，不影响自动回复上传图片。"),
        ),
    ),
    FeatureSpec(
        "reply",
        "自动回复",
        "管理普通关键词、包含关键词和正则回复。规则在下方逐条维护，指定成员回复在独立页面维护。",
        (
            b("enabled", "启用自动回复", False, "关闭后不匹配普通自动回复规则；不会删除下方已经保存的规则。"),
            i("cooldown", "同一成员回复冷却（秒）", 0, 0, 86400, "0 表示不限制。冷却按成员和命中的规则计算。"),
        ),
    ),
    FeatureSpec(
        "player_reply",
        "用户专属回复",
        "为指定 QQ 成员设置候选回复。它与普通自动回复分开匹配，避免相似名称造成重复配置。",
        (
            b("enabled", "启用指定成员回复", False, "关闭后不触发指定成员回复，已保存内容不会删除。"),
            b("default_at_user", "默认 @ 指定成员", True, "新建规则时的默认值；每条指定成员回复仍可单独选择是否 @。"),
        ),
    ),
    FeatureSpec(
        "stats",
        "统计与签到",
        "配置签到、签到状态、签到日历、签到排行、积分排行、个人信息和群统计。",
        (
            b("enabled", "启用统计和签到"),
            i("update_interval", "统计更新间隔（秒）", 300, 10, 86400),
            b("auto_update_on_join", "进群时更新统计", True),
            i("abnormal_invite_threshold", "异常邀请阈值", 5, 1, 1000),
            i("signin_base_reward", "每日签到积分", 10, 0, 1_000_000),
            i("signin_reward_3_days", "连续 3 天奖励", 30, 0, 1_000_000),
            i("signin_reward_7_days", "连续 7 天奖励", 100, 0, 1_000_000),
            i("signin_reward_14_days", "连续 14 天奖励", 200, 0, 1_000_000),
            i("signin_reward_30_days", "连续 30 天奖励", 500, 0, 1_000_000),
            choice(
                "signin_mode",
                "签到方式",
                "basic",
                (("basic", "普通签到"), ("steam", "绑定 SteamID 后签到")),
                "普通签到保留原有本地积分；SteamID 模式要求成员先绑定唯一的 17 位 SteamID，奖励和签到记录写入当前群的 SCUM 数据库。",
            ),
            choice(
                "schedule_mode",
                "签到周期",
                "daily",
                (("daily", "每日签到"), ("weekly", "每周签到"), ("monthly", "每月签到"), ("streak", "连续签到")),
                "签到记录按所选周期判断重复；连续签到模式会按连续天数使用阶梯奖励。旧配置中的 custom 仍可读取，但新配置不再生成无法独立定义规则的周期。",
            ),
            s("signin_command", "签到命令", "签到", "默认一个命令；需要多个命令时用 | 分隔，例如 签到|打卡|今日签到；不带斜杠和空格。首个命令用于帮助展示。", 256),
            s("bind_command", "SteamID 绑定命令", "绑定SteamID", "默认一个命令；需要多个命令时用 | 分隔，例如 绑定SteamID|绑定Steam；不带斜杠和空格。首个命令用于帮助展示。", 256),
            b("allow_group_steam_binding", "允许群内提交 SteamID", False, "默认关闭以保护 SteamID。成员应私聊机器人：/绑定SteamID 群号 17位SteamID。开启后允许群内使用绑定命令。"),
            b("recall_group_steam_binding_message", "撤回群内 SteamID 消息", True, "仅在“允许群内提交 SteamID”开启时生效；机器人会撤回成员的原始绑定消息，避免 SteamID 长时间留在群聊。"),
            i("group_steam_binding_recall_seconds", "群内 SteamID 撤回延迟（秒）", 5, 0, 600, "0 表示尽快撤回；建议保留 3-10 秒，便于成员确认机器人已收到。"),
            b("unbind_steam_on_leave", "退群后删除 SteamID 绑定", False, "默认保留绑定；开启后成员主动退群时删除本群 QQ 与 SteamID 关联，重新进群需重新验证。"),
            b("steamid_verification_required", "强制 SteamID 验证", False, "开启后，未绑定有效 SteamID 的普通群消息会被拦截并禁言；绑定命令、入群验证回答和管理员不受影响。"),
            i("unverified_mute_duration", "未验证发言禁言时长（秒）", 300, 1, 2592000, "未绑定 SteamID 的成员发言时自动禁言；建议 60-3600 秒。"),
            text("unverified_mute_message", "未验证禁言提示", "请先完成 SteamID 验证后再发言。", "留空不发送提示；支持 {user_id}、{user_name}、{duration}、{group_id}。"),
            items("custom_rewards", "自定义奖励说明", [], "每行一条奖励规则说明。用于向成员展示，实际积分仍由 Windows 按签到奖励字段结算。"),
            items("signin_profiles", "多个签到方案", default_steam_signin_profiles(), "已提供一条禁用的 SteamID 签到示例；修改数据库或 WebSocket 设置并确认后，再手动启用。方案命令不能与当前签到或 SteamID 绑定命令重复。"),
            text("signin_message", "签到成功文案", "{name} 的签到成功，第 {count} 次，奖励 {reward}。", "支持 {name}、{user_name}、{qq}、{steam_id}、{reward}、{balance}、{database_balance}、{count}、{month_count}、{week_count}、{streak}、{time}、{date}。"),
            text("bind_success_message", "SteamID 绑定成功文案", "绑定成功：{name} -> {steam_id}", "支持 {name}、{user_name}、{qq}、{steam_id}、{time}、{date}。"),
            text("bind_failure_message", "SteamID 绑定失败文案", "SteamID 绑定失败：{message}", "支持 {name}、{user_name}、{qq}、{steam_id}、{message}、{code}、{time}、{date}。"),
            text("unbind_success_message", "SteamID 解绑成功文案", "已解除 QQ {user_id} 的 SteamID 绑定。"),
            text("unbind_failure_message", "SteamID 解绑失败文案", "SteamID 解绑失败：{message}"),
            text("signin_reset_success_message", "签到重置成功文案", "签到重置成功：{user_id} {date}（{detail}）。"),
            text("signin_reset_failure_message", "签到重置失败文案", "签到重置失败：{message}"),
            text("duplicate_steam_message", "SteamID 已被绑定文案", "该 SteamID 已被其他 QQ 绑定，请联系管理员解绑。", "成员试图绑定已属于其他 QQ 的 SteamID 时使用。"),
            text("steam_not_found_message", "SteamID 不存在文案", "未找到该 SteamID，请确认已加入当前服务器。", "成员提交的 SteamID 不在当前服务器 user_list 表时使用。支持 {name}、{user_name}、{qq}、{steam_id}。"),
            text("steam_database_error_message", "SteamID 数据库错误文案", "SteamID 数据库暂时不可用，请联系管理员。", "当前服务器数据库无法读取或缺少 user_list.steam_id 时使用。支持 {name}、{user_name}、{qq}、{steam_id}。"),
            text("database_reward_error_message", "Steam 奖励写入失败文案", "奖励发放失败，本次签到未完成，请稍后重试。", "奖励未能写入当前群数据库时使用。支持 {name}、{user_name}、{qq}、{steam_id}、{reward}。"),
            text("database_already_signed_message", "数据库重复签到文案", "你在当前周期已经签到过了。", "sign_log 已有当前周期签到时使用。"),
            s("database_path", "数据库快照路径", "", "由“服务器与数据库”自动同步；没有绑定服务器数据库时不使用本地快照。", 1024),
            s("database_profile", "当前服务器数据库", "", "由分群“服务器与数据库”自动选择；留空时使用当前正在使用的服务器。无需手填内部编号。", 64),
            items("database_profiles", "服务器数据库列表", default_database_profiles(), "由分群“服务器与数据库”自动同步。每条记录对应一台服务器的远程 FTP SQLite 数据库和 Windows 本地快照，不在此处手填路径或 FTP 密码。"),
            choice(
                "currency_column",
                "Steam 奖励余额字段",
                "amount",
                (("amount", "熊币 amount"), ("integral", "积分 integral"), ("normal_integral", "普通积分 normal_integral"), ("coins", "金币 coins"), ("fragments", "碎片 fragments"), ("dlb_num", "大喇叭 dlb_num")),
                "默认使用数据库现有签到逻辑的 user_list.amount；只允许这些固定字段。",
            ),
            choice(
                "steam_reward_mode",
                "Steam 签到奖励方式",
                "database",
                (("database", "关联数据库增加熊币"), ("websocket", "WebSocket 执行游戏代码")),
                "普通签到只增加群管本地积分；SteamID 签到可选择数据库奖励或游戏代码奖励。",
            ),
            choice(
                "steam_reward_operation",
                "WebSocket 游戏操作",
                "execute_game_command",
                (("execute_game_command", "执行游戏指令"), ("steam_reward", "Steam 奖励指令")),
                "只使用 Windows 执行端已实现的操作；两者都会在执行成功后才记录签到。",
            ),
            text(
                "steam_reward_command",
                "Steam 奖励游戏指令",
                "#ChangeCurrencyBalance Normal {reward} {steam_id}",
                "支持 {reward}、{steam_id}、{qq}、{name}、{group_id}；SteamID 始终取当前群绑定值，不能由成员覆盖。",
            ),
            text(
                "steam_reward_failure_message",
                "游戏奖励失败文案",
                "签到奖励执行失败，未完成本次签到，请稍后重试。",
                "只有游戏执行端返回成功后才会写入签到记录。",
            ),
            b("database_sync", "普通签到也同步数据库", False, "兼容旧配置：开启后普通签到也要求绑定 SteamID 并写入当前群数据库；SteamID 签到请使用上方奖励方式。"),
        ),
    ),
    FeatureSpec(
        "lottery",
        "抽奖",
        "配置群内抽奖命令、抽取次数和回复文案。奖项清单由 Windows 抽奖功能单独维护。",
        (
            b("enabled", "启用抽奖", False, "关闭后机器人不处理本群抽奖命令，已保存的奖项和抽取记录不会删除。"),
            s("keyword", "抽奖命令", "抽奖", "默认一个命令；需要多个命令时用 | 分隔，例如 抽奖|摇奖|抽奖啦；不带斜杠和空格。首个命令用于帮助展示。", 256),
            i("max_draws_per_user", "每位成员最多抽取次数", 0, 0, 100_000, "0 表示不限制。限制按本群和成员分别计算。"),
            i("cooldown", "同一成员抽奖冷却（秒）", 0, 0, 86_400, "0 表示不限制；冷却期间会由 Windows 拒绝重复抽取。"),
            text(
                "success_message",
                "中奖回复",
                "🎉 {name} 抽中了「{prize}」！剩余 {remaining}",
                "可用变量：{name}、{user_name}、{qq}、{user_id}、{time}、{date}、{group}、{group_id}、{prize}、{award}、{remaining}、{stock}、{draw_count}、{count}；未知变量会原样保留。",
            ),
            text(
                "empty_message",
                "未中奖回复",
                "{name} 很遗憾没有抽中，再试一次吧。",
                "支持与中奖回复相同的变量；奖项耗尽时由 Windows 使用该文案。",
            ),
            items("campaigns", "多个抽奖活动", [], "由网页的“多个抽奖活动”子页面逐项维护；每个活动使用独立指令、次数限制、冷却、文案和奖项。"),
        ),
    ),
    FeatureSpec(
        "entertainment",
        "娱乐",
        "提供时间、每日批量点赞、音乐名片、视频搜索、骰子、猜拳、随机选择、抛硬币、今日运势、幸运数字和答案之书等轻量群互动。每个项目可独立开关和设置命令；结果在 Windows 本地执行，不写入 Steam/游戏数据库。",
        (
            b("enabled", "启用娱乐功能", False, "关闭后不处理娱乐命令；不会删除命令配置。"),
            b("time_enabled", "启用当前时间", True),
            b("dice_enabled", "启用骰子", True),
            b("rps_enabled", "启用猜拳", True),
            b("random_enabled", "启用随机选择", True),
            b("coin_enabled", "启用抛硬币", True, "使用 /抛硬币 随机得到正面或反面。"),
            b("fortune_enabled", "启用今日运势", True, "使用 /今日运势 获取一条随机运势；不写入个人资料。"),
            b("luck_enabled", "启用幸运数字", True, "使用 /幸运数字 获取按成员和日期固定的每日数字。"),
            b("answer_enabled", "启用答案之书", True, "使用 /答案之书 问一个问题，获得一条随机建议；不会收集问题内容。"),
        b("like_enabled", "启用 QQ 点赞", True, "使用 OneBot send_like 批量点赞；/赞我 默认给发言者本人点赞，也支持 QQ 号或 @成员；目标需要是机器人好友。"),
            b("music_enabled", "启用点歌搜索", True, "按本群默认音乐源搜索；/点歌 返回候选，/选歌 序号发送音乐名片。"),
            multi_choice(
                "music_sources",
                "音乐源平台",
                ["qq", "163"],
                (
                    ("qq", "QQ音乐"),
                    ("163", "网易云音乐"),
                    ("kugou", "酷狗音乐"),
                    ("soda", "汽水音乐（需后台服务）"),
                ),
                "本群独立设置。默认只搜索下方选定的平台；可在歌名前加 QQ、网易云、酷狗或汽水临时指定已启用的平台。汽水需要管理员先在“音乐源服务”配置兼容接口。",
                group_only=True,
            ),
            choice(
                "music_default_source",
                "默认点歌平台",
                "qq",
                (
                    ("qq", "QQ音乐"),
                    ("163", "网易云音乐"),
                    ("kugou", "酷狗音乐"),
                    ("soda", "汽水音乐"),
                ),
                "不带平台前缀的 /点歌 只查询这里选定的平台。该平台必须同时在“音乐源平台”中启用。",
                group_only=True,
            ),
            b("video_enabled", "启用视频搜索", True, "返回哔哩哔哩和 YouTube 搜索链接，不依赖外部视频插件。"),
            s("time_command", "时间命令", "当前时间", "默认一个命令；需要多个命令时用 | 分隔，例如 当前时间|时间；不带斜杠和空格。", 256),
            s("dice_command", "骰子命令", "骰子", "默认一个命令；需要多个命令时用 | 分隔，例如 骰子|掷骰；不带斜杠和空格。", 256),
            s("rps_command", "猜拳命令", "猜拳", "默认一个命令；需要多个命令时用 | 分隔，例如 猜拳|石头剪刀布；不带斜杠和空格。", 256),
            s("random_command", "随机命令", "随机", "默认一个命令；不带斜杠和空格。群内使用空格分隔选项，例如：/随机 吃饭 睡觉 看电影。", 256),
            s("coin_command", "抛硬币命令", "抛硬币", "默认一个命令；需要多个命令时用 | 分隔，例如 抛硬币|硬币；不带斜杠和空格。", 256),
            s("fortune_command", "运势命令", "今日运势", "默认一个命令；需要多个命令时用 | 分隔，例如 今日运势|运势；不带斜杠和空格。", 256),
            s("luck_command", "幸运数字命令", "幸运数字", "默认一个命令；需要多个命令时用 | 分隔，例如 幸运数字|今日幸运；不带斜杠和空格。", 256),
            s("answer_command", "答案之书命令", "答案之书", "用法：/答案之书 问题；需要多个命令时用 | 分隔，例如 答案之书|问一问；不带斜杠和空格。", 256),
            s("like_command", "点赞命令", "赞我", "用法：/赞我、/赞我 QQ号 或 @成员；目标需要是机器人好友；需要多个命令时用 | 分隔，例如 赞我|点赞。首个命令用于帮助展示。", 256),
            s("music_command", "点歌命令", "点歌", "用法：/点歌 歌名；需要多个命令时用 | 分隔，例如 点歌|搜歌；发送 /选歌 序号选择候选音乐。", 256),
            i("music_selection_timeout", "点歌等待选歌时间（秒）", 600, 10, 3600, "搜索结果保留时间；范围 10 秒至 1 小时，按本群和用户独立生效。"),
            s("video_command", "视频搜索命令", "搜索视频", "用法：/搜索视频 关键词；需要多个命令时用 | 分隔，例如 搜索视频|视频搜索；返回哔哩哔哩和 YouTube 搜索链接。", 256),
            i("like_daily_limit", "每日点赞上限", 20, 1, 20, "每个机器人账号单独计算；VIP 账号最多 20 次，未确认目标在好友列表或 QQ 未明确成功时不扣额度。"),
            i("cooldown", "成员冷却（秒）", 0, 0, 86_400, "0 表示不限制；按群和成员分别计算。"),
            i("max_sides", "骰子最大面数", 100, 2, 100_000),
            i("max_options", "随机选项上限", 20, 2, 50),
        ),
    ),
    FeatureSpec(
        "scum",
        "SCUM 查询",
        "普通分群仅可绑定服务器ID并查询在线状态。关键词搜索仅机器人主人或白名单群可用。",
        (
            # Keep this enabled by default for existing groups: the Windows
            # plugin historically exposed the three query commands whenever
            # ``scum.enable_query`` was enabled.  Customers can disable the
            # feature per group from the portal without changing the global
            # plugin setting.
            b("enabled", "启用 SCUM 查询", True),
            b(
                "allow_server_binding",
                "允许绑定服务器ID",
                True,
                "关闭后本群不能绑定或解绑服务器ID。绑定仍要求群管理权限。",
                group_only=True,
            ),
            b(
                "allow_online_query",
                "允许查询在线状态",
                True,
                "开启后本群成员可使用在线查询指令；需先绑定服务器ID。",
                group_only=True,
            ),
            choice(
                "permission_mode",
                "权限模式",
                "read_only",
                (("read_only", "只读"), ("full_access", "完全访问")),
                "完全访问时群主和管理员可绑定服务器；只读时仅机器人主人可绑定。",
            ),
            s("keyword_command", "关键词查询命令", "查询", "仅机器人主人或白名单群可用；不带斜杠和空格。", 256),
            s("online_command", "在线查询命令", "查询在线", "普通分群可用；需先绑定服务器ID。不带斜杠和空格。", 256),
        ),
    ),
    FeatureSpec(
        "keyword_filter",
        "关键词过滤",
        "命中关键词或正则后执行警告、撤回、禁言、踢出或拉黑。规则逐条保存到本群，并由 Windows 运行。",
        (
            b("enabled", "启用关键词过滤", False, "关闭后不会处理新消息，关键词和正则配置会保留。"),
            choice(
                "filter_action",
                "处理动作",
                "warn",
                (
                    ("warn", "警告"),
                    ("delete", "仅撤回"),
                    ("delete_warn", "撤回并警告"),
                    ("mute", "禁言"),
                    ("kick", "踢出"),
                    ("blacklist", "拉黑"),
                ),
                "“撤回并警告”会先尝试撤回命中消息，再 @ 成员发送下方警告文案；若平台不返回消息 ID，警告仍会发送。",
            ),
            i("filter_mute_duration", "禁言时长（秒）", 300, 1, 2_592_000, "只在处理动作为“禁言”时使用。"),
            items("filter_keywords", "过滤关键词", ["广告", "推广", "二维码", "vx", "微信", "qq", "群号", "拉人"], "每行一个普通关键词；消息包含任一项即命中。可在群内使用“/查看过滤词列表”查看当前内容。"),
            items("filter_regex_patterns", "过滤正则", [], "每行一个 Python 正则，例如 (?i)广告|推广；不要写前后 /。正则只匹配文本。"),
            b("admin_bypass_filter", "管理员免过滤", True, "开启后群主和群管理员的消息不参与关键词过滤。"),
            b("enable_log", "记录命中日志", True, "将命中动作写入 Windows 运行日志，方便在“消息收发”筛选。"),
            text("warn_message", "警告文案", "您的消息包含敏感内容，请遵守群规！", "可用 " + TEMPLATE_VARIABLE_HELP + "“警告”和“撤回并警告”都会使用此文案。"),
            items("warning_messages", "警告文案（多条随机）", ["您的消息包含敏感内容，请遵守群规！"], "每行一条警告文案；" + TEMPLATE_VARIABLE_HELP + "命中“警告”或“撤回并警告”时由 Windows 随机选择。"),
            text("mute_message", "禁言文案", "{user_id} 已被禁言，请遵守群规。", "处理动作是“禁言”时发送；" + TEMPLATE_VARIABLE_HELP),
            text("kick_message", "踢出文案", "{user_id} 已被移出本群。", "处理动作是“踢出”时发送；" + TEMPLATE_VARIABLE_HELP),
            text("blacklist_message", "拉黑文案", "{user_id} 已加入黑名单。", "处理动作是“拉黑”时发送；" + TEMPLATE_VARIABLE_HELP),
            b("repeat_user_enabled", "启用单人刷屏检测", False, "同一成员在时间窗内连续发送消息达到次数后处理。命令消息、机器人消息和管理员消息（开启免过滤时）不会计数。"),
            i("repeat_user_threshold", "单人刷屏次数", 3, 2, 100, "同一成员在时间窗内发送消息达到此次数才处理，不要求内容完全相同。"),
            i("repeat_user_window", "单人刷屏时间窗（秒）", 60, 5, 3600, "只统计最近这段时间内的消息。"),
            choice(
                "repeat_user_action",
                "单人刷屏动作",
                "delete",
                (("delete", "撤回"), ("mute", "禁言"), ("delete_mute", "撤回并禁言")),
                "默认只撤回；只有明确选择“禁言”或“撤回并禁言”才会禁言。禁言时长使用下方统一设置。",
            ),
            b("repeat_group_enabled", "启用多人刷屏检测", False, "不同成员在时间窗内发送相同内容达到人数后处理。用于集中刷屏或复制同一广告。"),
            i("repeat_group_threshold", "多人刷屏人数", 3, 2, 100, "发送相同内容的不同成员达到此人数才处理。"),
            i("repeat_group_window", "多人刷屏时间窗（秒）", 60, 5, 3600, "只统计最近这段时间内的消息。"),
            choice(
                "repeat_group_action",
                "多人刷屏动作",
                "delete",
                (("delete", "撤回"), ("mute", "禁言"), ("delete_mute", "撤回并禁言")),
                "默认只撤回；只有明确选择“禁言”或“撤回并禁言”才会禁言。动作会作用于当前发送者。",
            ),
            i("repeat_mute_duration", "刷屏禁言时长（秒）", 300, 1, 2_592_000, "单人或多人刷屏动作包含禁言时使用。"),
            text("repeat_notice", "刷屏处理提示", "检测到重复刷屏，已处理。", "触发刷屏处理后发送；留空会自动使用这条默认提示。"),
        ),
    ),
    FeatureSpec(
        "timed_message",
        "定时消息",
        "管理文字定时播报、随机音乐播报、全体禁言和全体解禁任务。具体任务在下方逐条维护。",
        (b("enabled", "启用定时任务", False, "关闭后所有本群定时任务暂停执行，但每条任务及其启用状态会保留。"),),
    ),
    FeatureSpec(
        "game_commands",
        "游戏指令",
        "按群保存 SCUM 游戏指令模板；支持任意合法 SCUM 代码和多段代码，运行时只会把当前发言人的本群 SteamID 填入模板。可恢复金币和生成僵尸的默认模板，不会清除绑定、签到记录或服务器密码。",
        (
            b("enabled", "启用游戏指令", False, "关闭后不会处理以 # 开头的游戏指令。"),
            text("success_message", "执行成功回复", "游戏指令执行成功：{message}", "支持 {message}、{result}、{name}、{qq}、{steam_id}、{group_id}；留空使用系统默认回复。"),
            text("failure_message", "执行失败回复", "游戏指令执行失败：{reason}", "支持 {reason}、{code}、{name}、{qq}、{steam_id}、{group_id}；留空使用系统默认失败原因。"),
            choice(
                "execution_mode",
                "游戏指令执行方式",
                "local",
                (
                    ("local", "Windows 本地执行"),
                    ("websocket", "使用后台 WebSocket"),
                ),
                "选择 WebSocket 后，本群的游戏指令会通过后台已配置的 WebSocket 转发到 Windows 执行端；不会使用其他群的连接。",
            ),
            items(
                "commands",
                "游戏指令模板",
                [],
                "每行一个 JSON 对象或一条完整代码；完整代码可用逗号或 | 分隔多段，例如 #ChangeCurrencyBalance Normal 10000000|#ChangeGoldBalance 20。含 location 时在其后插入本群绑定 SteamID，没有 location 时追加 SteamID；也支持 {steam_id}、{location}、{qq}、{name}、{group_id}。数据库余额操作可将 operation 设为 database_credit 或 database_debit，并用 currency_column 选择 amount/coins/fragments 等字段，只有 SQLite 提交成功才回复成功。",
            ),
        ),
    ),
    FeatureSpec(
        "game_servers",
        "游戏插件连接",
        "在 Linux 网页为本群配置多个 SCUM 插件 WebSocket 链接，并为每台服务器关联对应的 FTP/SQLite 数据库。Windows 负责检测、连接和按群执行。",
        (
            b("enabled", "启用本群游戏服务器配置", False, "开启后，本群的游戏指令和 WebSocket Steam 奖励会使用下方档案。"),
            items(
                "profiles",
                "游戏插件 WebSocket 连接",
                [],
                "在下方逐台添加，不需要填写 JSON。每台服务器只填写开发文档中的 WebSocket 链接；API Key 通过 HTTPS 加密同步到 Windows。FTP/数据库通过档案 ID 关联。",
            ),
            s("active_profile_id", "当前使用的服务器", "", "多台服务器时选择游戏指令和签到奖励实际发送到哪一台；留空使用第一条启用服务器。", 48),
        ),
    ),
    FeatureSpec(
        "sub_admin",
        "分群管理员",
        "把本群内的部分管理权限授予指定 QQ 用户，不影响其他群。",
        (b("enabled", "启用分群管理员", False, "关闭后指定成员不再获得分群权限；成员列表和权限勾选会保留。"),),
    ),
    FeatureSpec(
        "group_config",
        "群设置",
        "维护本群的显示名称、标签和优先级等基础资料；这些资料会与 Windows 端分群配置一起同步。",
        (
            b("enabled", "启用群设置", True, "关闭后仅隐藏群设置指令，不会删除已经保存的基础资料。"),
            s("help_command", "群管目录命令", "群管功能", "用于打开本群功能目录；需要多个名称时用 | 分隔，不带斜杠和空格。旧的 /群管功能 等名称仍保留兼容。", 256),
            choice(
                "help_permission",
                "群管目录使用权限",
                "member",
                (("member", "所有成员"), ("admin", "群管理员/群主"), ("owner", "群主/机器人主人")),
                "控制谁可以查看本群实际指令用法。",
            ),
            s("nickname", "群显示名称", "", "用于网页标题、群列表和机器人日志；留空时使用 QQ 群号。", 100),
            s("database_player_name", "服务器数据库初始玩家名称", "", "入群绑定 SteamID 后写入服务器 user_list 的名称；留空自动使用群显示名称。", 255),
            b("steam_name_as_group_card", "SteamID 绑定后初始化群名片", True, "开启后，SteamID 验证/绑定成功时，把数据库核验得到的游戏名字设置为 QQ 群名片；这与服务器数据库玩家名称是两项独立设置。"),
            items("tags", "群标签", [], "每行一个标签，例如：活动群、管理群；标签只用于筛选和识别。"),
            i("priority", "配置优先级", 0, -1000, 1000, "数值越大越优先，用于多个群配置批量管理时排序。"),
            text("bot_account_priorities", "同群机器人优先级", "", "一行一个：机器人QQ=优先级，例如 123456=100。多个账号在同一群时，数值最高的账号执行定时任务；未填写的账号使用 0。"),
            text("group_note", "群备注", "", "仅供管理员和群主查看的备注，不会发送到群聊。"),
            s("timezone", "群时区", "Asia/Shanghai", "定时消息和签到显示使用的 IANA 时区名称。", 64),
        ),
    ),
    FeatureSpec(
        "text_to_image",
        "文转图",
        "将机器人文字回复渲染为图片。网页只管理文转图样式；图转文由群内命令处理，不增加没有运行时支持的网页开关。",
        (
            b("enabled", "启用文转图"),
            i("font_size", "字号", 16, 8, 96),
            s("font_color", "文字颜色", "#333333", "CSS 十六进制颜色", 7),
            s("bg_color", "背景颜色", "#ffffff", "CSS 十六进制颜色", 7),
            i("line_spacing", "行距", 4, 0, 100),
            i("padding", "边距", 20, 0, 200),
            choice(
                "modify_permission",
                "修改权限",
                "admin",
                (("admin", "群管理员"), ("owner", "仅群主")),
                "控制谁可以在群内修改文转图设置。",
            ),
        ),
    ),
)


# Member card/title rules are kept in the group-config feature rather than in
# runtime statistics.  They are deliberately JSON-line lists so one rule can
# be edited without exposing the local member database to the portal.
_MEMBER_IDENTITY_FIELDS: tuple[FieldSpec, ...] = (
    b(
        "auto_member_name",
        "自动设置群员名称",
        False,
        "成员入群或发消息时，按方案设置群名片；仅处理已分配方案的 QQ。",
    ),
    s(
        "member_name_prefix",
        "群名片全局前缀",
        "",
        "填写后会先移除旧前缀，再重新生成一次群名片：已绑定 SteamID 的成员使用验证通过的游戏名，未绑定成员使用 QQ 昵称；例如 scum- + 张先生 = scum-张先生。清空此项并保存即可清除前缀并恢复未绑定成员自己的昵称，避免出现多个嵌套前缀。",
        20,
    ),
    b(
        "member_name_prefix_include_admins",
        "群名片前缀修改群主/管理员",
        True,
        "关闭后，群名片全局前缀只应用于普通成员，不修改群主和管理员的群名片。",
    ),
    items(
        "member_names",
        "群员名称方案",
        [],
        "每行一个 JSON 对象，格式：{\\\"user_id\\\":\\\"123456\\\",\\\"name\\\":\\\"玩家\\\"}；也可使用 123456=玩家。user_id 填 *、all 或 所有人 可应用到所有成员；名称支持 {user_name}、{user_id}。",
    ),
    b(
        "auto_member_title",
        "自动授予群员称号",
        False,
        "默认关闭；开启后新成员或发言成员按规则授予称号。默认规则为全体成员=群成员；机器人必须是 QQ 群主，且 NapCat PacketBackend 已初始化。",
    ),
    items(
        "member_titles",
        "群员称号方案",
        ["*=群成员"],
        "每行一个 JSON 对象，格式：{\\\"user_id\\\":\\\"123456\\\",\\\"title\\\":\\\"勇者\\\"}；也可使用 123456=勇者。user_id 填 *、all 或 所有人 可应用到所有成员；称号支持 {user_name}、{user_id}。",
    ),
)
FEATURE_SPECS = tuple(
    FeatureSpec(
        spec.key,
        spec.label,
        spec.description,
        spec.fields + (_MEMBER_IDENTITY_FIELDS if spec.key == "group_config" else ()),
    )
    for spec in FEATURE_SPECS
)


def _catalog_label(key: str, fallback: str) -> str:
    category = GROUP_FEATURE_BY_KEY.get(key)
    return category.label if category is not None else fallback


# Keep the web configuration tabs aligned with the bot's canonical primary
# categories while retaining the portal-only message-cache category.
FEATURE_SPECS = tuple(
    FeatureSpec(
        spec.key,
        _catalog_label(spec.key, spec.label),
        spec.description,
        spec.fields,
    )
    for spec in FEATURE_SPECS
)


FEATURE_BY_KEY = {feature.key: feature for feature in FEATURE_SPECS}

# Keep legacy Windows policy mirrors editable from the portal as well.  These
# fields are consumed by qqadmin.blacklist/mute but were historically omitted
# from the web schema, so saving a feature silently discarded them.
_EXTRA_POLICY_FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    "blacklist": (
        b("auto_kick_on_join", "黑名单成员入群自动踢出", True),
        b("recall_on_kick", "手动拉黑后撤回该用户消息", True, "手动执行拉黑时，先撤回 Windows 消息缓存和 QQ 最近记录，再踢出用户。关闭后只拉黑并踢出，不主动撤回。"),
        b("notify_on_kick", "自动踢出后发送群提示", True),
        s("kick_reason", "自动踢出提示原因", "已被加入黑名单", max_length=300),
        i("max_auto_kick_per_day", "每日自动踢出上限", 10, 0, 1000, "黑名单自动踢人每天最多成功执行多少次；只统计自动踢出，不限制手动踢人。达到上限后仍保留黑名单记录，次日按 Windows 本地日期自动恢复额度；设置为 0 表示关闭自动踢出。"),
        text("auto_kick_message", "自动踢出回复", "黑名单用户 {user_id} 已被移出本群，原因：{reason}", TEMPLATE_VARIABLE_HELP),
        text("leave_blacklist_message", "退群拉黑回复", "用户 {user_name}({user_id}) 主动退群，已加入黑名单。", TEMPLATE_VARIABLE_HELP),
        text("blacklist_success_message", "拉黑成功回复", "已拉黑用户 {user_name}（{user_id}）。", TEMPLATE_VARIABLE_HELP),
        text("blacklist_failure_message", "拉黑失败回复", "拉黑失败：{reason}", TEMPLATE_VARIABLE_HELP),
        text("unblacklist_success_message", "解除拉黑回复", "已解除用户 {user_name}（{user_id}）的拉黑。", TEMPLATE_VARIABLE_HELP),
        text("unblacklist_failure_message", "解除拉黑失败回复", "解除拉黑失败：{reason}", TEMPLATE_VARIABLE_HELP),
    ),
    "mute": (
        b("auto_mute_new_member", "新成员自动禁言", False),
        i("auto_mute_duration", "新成员禁言时长（秒）", 300, 1, 2_592_000),
        b("auto_escalate", "重复违规自动升级", True),
        i("repeat_threshold", "每升一级所需禁言次数", 1, 1, 1000),
        i("repeat_extension", "未设置等级时长时每级增加秒数", 300, 0, 2_592_000),
        i("max_level", "最高禁言等级", 5, 1, 100),
        items("level_durations", "各等级禁言时长（秒）", [300, 600, 1800, 3600, 7200], "每行一个等级时长，第一行对应 1 级。"),
        items("mention_mute_rules", "艾特禁言规则", [], "每行一条，单人格式 QQ号=秒数表示有人艾特该 QQ 时禁言发言人；定向格式 触发QQ>禁言QQ=秒数，例如 123456789>987654321=300，表示消息中艾特前者时只禁言后者，不会禁言发言人。最长 30 天。"),
        text("mute_success_message", "禁言成功回复", "已禁言用户 {user_name}（{user_id}），时长：{duration}。", "支持 {user_name}、{user_id}、{group_id}、{duration}、{reason}。"),
        text("unmute_success_message", "解除禁言回复", "已解除用户 {user_name}（{user_id}）的禁言。", "支持 {user_name}、{user_id}、{group_id}。"),
        text("mute_failure_message", "禁言失败回复", "禁言失败：{reason}"),
    ),
    "kick": (
        text("kick_success_message", "踢出成功回复", "用户 {user_id} 已被移出群聊，原因：{reason}", TEMPLATE_VARIABLE_HELP),
        text("kick_failure_message", "踢出失败回复", "踢出失败：{reason}", TEMPLATE_VARIABLE_HELP),
    ),
}
FEATURE_SPECS = tuple(
    FeatureSpec(
        feature.key,
        feature.label,
        feature.description,
        feature.fields + _EXTRA_POLICY_FIELDS.get(feature.key, ()),
    )
    for feature in FEATURE_SPECS
)
FEATURE_BY_KEY = {feature.key: feature for feature in FEATURE_SPECS}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
COMMAND_FIELD_KEYS = {
    "keyword",
    "keyword_command",
    "online_command",
    "dice_command",
    "rps_command",
    "random_command",
    "coin_command",
    "fortune_command",
    "luck_command",
    "answer_command",
    "signin_command",
    "bind_command",
    "time_command",
    "like_command",
    "music_command",
    "video_command",
    "help_command",
}
SCUM_COMMAND_DEFAULTS = {
    "keyword_command": "查询",
    "online_command": "查询在线",
}
SCUM_COMMAND_ALIASES = {
    "查询", "查询1", "id查询", "查服",
    "查询在线", "在线查询",
}
SCUM_COMMAND_ALIAS_GROUPS = {
    "keyword_command": {"查询", "查询1", "id查询", "查服"},
    "online_command": {"查询在线", "在线查询"},
}
JSON_LINE_FIELD_KEYS = {"signin_profiles", "campaigns", "member_names", "member_titles", "database_profiles", "profiles", "commands"}
DATABASE_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
DATABASE_PROFILE_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
GAME_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
GAME_SERVER_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,253}$")
GAME_SERVER_EXECUTOR_TYPES = frozenset({"plugin_websocket"})


def _parse_plugin_websocket_url(value: object) -> dict[str, Any]:
    """Validate the plugin documentation URL without retaining it in clear text."""
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if (
        not text
        or len(text) > 2048
        or parsed.scheme.lower() not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path != "/ws"
        or parsed.fragment
    ):
        raise ValueError("插件 WebSocket 链接必须为 ws(s)://主机:端口/ws?apiKey=...")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("插件 WebSocket 链接端口无效") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("插件 WebSocket 链接必须包含 1-65535 的端口")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) != {"apiKey"} or len(query["apiKey"]) != 1:
        raise ValueError("插件 WebSocket 链接只能包含一个 apiKey 参数")
    api_key = str(query["apiKey"][0] or "").strip()
    if not api_key or len(api_key) > 512 or "\r" in api_key or "\n" in api_key:
        raise ValueError("插件 WebSocket API Key 无效")
    return {
        "host": str(parsed.hostname).strip(),
        "port": port,
        "api_key": api_key,
        "websocket_path": "/ws",
        "websocket_scheme": parsed.scheme.lower(),
    }


def normalize_game_server_profile_descriptor(value: Any) -> dict[str, Any]:
    """Validate one documented plugin WebSocket server profile."""
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("游戏服务器档案 JSON 格式无效") from None
    if not isinstance(parsed, dict):
        raise ValueError("游戏服务器档案必须是 JSON 对象")
    profile_id = str(parsed.get("id", parsed.get("profile_id", "")) or "").strip()
    if not GAME_SERVER_ID_RE.fullmatch(profile_id):
        raise ValueError("游戏服务器档案 id 只能使用字母、数字、点、下划线或短横线")
    name = str(parsed.get("name", profile_id) or profile_id).strip()
    if not name or len(name) > 80 or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ValueError("游戏服务器名称长度应为 1-80 个字符")
    websocket_url = str(parsed.get("websocket_url", "") or "").strip()
    websocket_values = _parse_plugin_websocket_url(websocket_url) if websocket_url else {}
    host = str(websocket_values.get("host", parsed.get("host", "")) or "").strip().strip("[]")
    if not host or len(host) > 255 or not GAME_SERVER_HOST_RE.fullmatch(host) or ".." in host:
        raise ValueError("游戏服务器 host 必须是 IP 或域名")
    try:
        port = int(websocket_values.get("port", parsed.get("port", 28016)))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("游戏服务器 port 必须是 1-65535 的整数") from None
    if not 1 <= port <= 65535:
        raise ValueError("游戏服务器 port 必须在 1-65535 之间")
    executor_type = str(parsed.get("type", "plugin_websocket") or "plugin_websocket").strip().lower()
    if websocket_values:
        executor_type = "plugin_websocket"
    if executor_type not in GAME_SERVER_EXECUTOR_TYPES:
        raise ValueError("游戏服务器连接类型只能使用插件 WebSocket")
    try:
        timeout = float(parsed.get("timeout_seconds", 10) or 10)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("游戏服务器超时时间格式无效") from None
    if not 1 <= timeout <= 30:
        raise ValueError("游戏服务器超时时间必须在 1-30 秒之间")
    api_key = str(websocket_values.get("api_key", parsed.get("api_key", "")) or "")
    if len(api_key) > 512 or "\r" in api_key or "\n" in api_key:
        raise ValueError("插件 WebSocket API Key 格式无效")
    websocket_path = str(websocket_values.get("websocket_path", parsed.get("websocket_path", "/ws")) or "/ws").strip()
    if websocket_path != "/ws":
        raise ValueError("插件 WebSocket 路径必须是 /ws")
    websocket_scheme = str(
        websocket_values.get(
            "websocket_scheme", parsed.get("websocket_scheme", "ws")
        )
        or "ws"
    ).strip().lower()
    if websocket_scheme not in {"ws", "wss"}:
        raise ValueError("插件 WebSocket 协议只能是 ws 或 wss")
    integration_profile_id = str(
        parsed.get("integration_profile_id", parsed.get("database_profile", "")) or ""
    ).strip()
    if integration_profile_id and not DATABASE_PROFILE_ID_RE.fullmatch(integration_profile_id):
            raise ValueError("关联 FTP/服务器数据库 ID 格式无效")
    enabled = parsed.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"", "0", "false", "off", "no"}
    groups = parsed.get("groups", parsed.get("group_ids", []))
    if isinstance(groups, str):
        groups = [item for item in re.split(r"[\s,，]+", groups.strip()) if item]
    elif not isinstance(groups, (list, tuple, set)):
        groups = []
    normalized_groups = []
    for group_id in groups:
        group_id = str(group_id or "").strip()
        if group_id and group_id.isdigit() and len(group_id) <= 32 and group_id not in normalized_groups:
            normalized_groups.append(group_id)
    result = {
        "id": profile_id,
        "name": name,
        "enabled": bool(enabled),
        "type": executor_type,
        "host": host,
        "port": port,
        "api_key": api_key,
        "websocket_path": websocket_path,
        "websocket_scheme": websocket_scheme,
        "timeout_seconds": timeout,
        "integration_profile_id": integration_profile_id,
    }
    if normalized_groups:
        result["groups"] = normalized_groups[:256]
    return result


def _canonical_game_server_profile_line(field: FieldSpec, item: str) -> str:
    try:
        profile = normalize_game_server_profile_descriptor(item)
    except ValueError as exc:
        raise ValueError(f"{field.label}：{exc}") from None
    return json.dumps(profile, ensure_ascii=False, separators=(",", ":"))


def _canonical_member_rule_line(field: FieldSpec, item: str) -> str:
    """Validate and canonicalize one member card/title rule.

    Keeping this check in the web parser gives the operator an actionable form
    error instead of silently accepting a row that Windows cannot execute.
    """
    if field.key not in {"member_names", "member_titles"}:
        return item
    value_key = "name" if field.key == "member_names" else "title"
    if item.startswith("{"):
        try:
            parsed = json.loads(item)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError(f"{field.label}中的 JSON 格式无效") from None
        if not isinstance(parsed, dict):
            raise ValueError(f"{field.label}每行必须是 JSON 对象")
        user_id = parsed.get("user_id") or parsed.get("userId") or parsed.get("qq") or parsed.get("id")
        display = parsed.get(value_key)
    elif "=" in item:
        user_id, display = item.split("=", 1)
    else:
        raise ValueError(f"{field.label}格式应为 QQ=名称 或 JSON 对象")
    user_id = str(user_id or "").strip()
    display = str(display or "").replace("\r", " ").replace("\n", " ").strip()
    wildcard = user_id.casefold() in {"*", "all", "everyone", "全体", "所有人"}
    if (not wildcard and (not re.fullmatch(r"[0-9]+", user_id) or int(user_id) <= 0 or len(user_id) > 20)):
        raise ValueError(f"{field.label}包含无效 QQ 号或通配符")
    if wildcard:
        user_id = "*"
    limit = 60 if field.key == "member_names" else 18
    if not display or len(display) > limit or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in display):
        raise ValueError(f"{field.label}中的名称长度应为 1-{limit} 个字符")
    return json.dumps(
        {"user_id": user_id, value_key: display},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def normalize_database_profile_descriptor(value: Any) -> dict[str, Any]:
    """Return the only database-profile fields allowed on the sync wire."""

    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("服务器数据库配置 JSON 格式无效") from None
    if not isinstance(parsed, dict):
        raise ValueError("服务器数据库配置必须是 JSON 对象")
    profile_id = str(parsed.get("id", parsed.get("profile_id", "")) or "").strip()
    if not DATABASE_PROFILE_ID_RE.fullmatch(profile_id):
        raise ValueError("服务器数据库 ID 只能使用 1-48 位字母、数字、_ 或 -")
    name = str(parsed.get("name", profile_id) or profile_id).replace("\r", " ").replace("\n", " ").strip()
    if not name or len(name) > 80 or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ValueError("服务器数据库名称长度应为 1-80 个字符")

    server_id = str(parsed.get("server_id", "") or "").strip()
    if server_id and not DATABASE_PROFILE_SERVER_ID_RE.fullmatch(server_id):
        raise ValueError("服务器数据库的服务器 ID 格式无效")

    path = str(
        parsed.get("database_path", parsed.get("path", parsed.get("sqlite_path", "")))
        or ""
    ).strip().replace("\\", "/")
    path_parts = [part for part in path.split("/") if part not in {"", "."}]
    if (
        not path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path)
        or ".." in path_parts
        or "\x00" in path
        or any(":" in part or any(ord(char) < 32 for char in part) for part in path_parts)
        or len(path) > 512
    ):
        raise ValueError("服务器数据库快照路径必须是 Windows 插件目录内的相对路径")

    enabled = parsed.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"", "0", "false", "off", "no"}
    result = {
        "id": profile_id,
        "name": name,
        "server_id": server_id,
        "enabled": bool(enabled),
        "path": "/".join(path_parts),
    }
    # Portal-managed FTP snapshots carry an optional revision/checksum.  Keep
    # these fields absent for legacy/manual descriptors so existing wire
    # payloads remain byte-for-byte compatible, while validating any values
    # supplied by the portal before they cross the sync boundary.
    if "snapshot_revision" in parsed or "snapshot_sha256" in parsed:
        try:
            snapshot_revision = int(parsed.get("snapshot_revision", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("数据库快照版本无效") from None
        if snapshot_revision < 0 or snapshot_revision > 2**31 - 1:
            raise ValueError("数据库快照版本无效")
        snapshot_sha256 = str(parsed.get("snapshot_sha256", "") or "").strip().lower()
        if snapshot_sha256 and not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256):
            raise ValueError("数据库快照校验值无效")
        result["snapshot_revision"] = snapshot_revision
        result["snapshot_sha256"] = snapshot_sha256
    return result


def _canonical_database_profile_line(field: FieldSpec, item: str) -> str:
    """Return the public Windows descriptor for one database profile.

    FTP credentials and connection strings are Linux-owned encrypted data.
    The free-form legacy field therefore uses a strict allowlist before a row
    can enter the group projection or the Windows synchronization payload.
    """

    try:
        public = normalize_database_profile_descriptor(item)
    except ValueError as exc:
        raise ValueError(f"{field.label}：{exc}") from None
    return json.dumps(public, ensure_ascii=False, separators=(",", ":"))


def parse_field_value(
    field: FieldSpec,
    raw: str | list[str] | tuple[str, ...] | None,
    present: bool = True,
) -> Any:
    if field.kind == "bool":
        return bool(present and str(raw or "").lower() in {"1", "true", "on", "yes"})

    if field.kind == "multichoice":
        if not present:
            return list(field.default)
        raw_values = raw if isinstance(raw, (list, tuple)) else [raw]
        allowed = {option[0] for option in field.choices}
        values: list[str] = []
        for raw_value in raw_values:
            value = str(raw_value or "").strip()
            if value not in allowed:
                raise ValueError(f"{field.label}选项无效")
            if value not in values:
                values.append(value)
        if not values:
            raise ValueError(f"{field.label}至少选择一项")
        return values

    value = (raw or "").strip()
    if len(value) > field.max_length:
        raise ValueError(f"{field.label}内容过长")

    if field.kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field.label}必须是整数") from None
        if field.minimum is not None and number < field.minimum:
            raise ValueError(f"{field.label}不能小于 {field.minimum}")
        if field.maximum is not None and number > field.maximum:
            raise ValueError(f"{field.label}不能大于 {field.maximum}")
        return number

    if field.kind == "choice":
        allowed = {option[0] for option in field.choices}
        if value not in allowed:
            raise ValueError(f"{field.label}选项无效")
        return value

    if field.kind == "list":
        lines = value.replace("，", ",").splitlines()
        values: list[str] = []
        for line in lines:
            # Multi-instance definitions are one JSON object per line; commas
            # inside the object are data, not list separators.
            candidates = (
                [line]
                if field.key in JSON_LINE_FIELD_KEYS
                else (line.split(",") if "," in line else [line])
            )
            for candidate in candidates:
                item = candidate.strip()
                if item and field.key in {"member_names", "member_titles"}:
                    item = _canonical_member_rule_line(field, item)
                elif item and field.key == "database_profiles":
                    item = _canonical_database_profile_line(field, item)
                elif item and field.key == "profiles":
                    item = _canonical_game_server_profile_line(field, item)
                if item and item not in values:
                    values.append(item)
        if len(values) > 200:
            raise ValueError(f"{field.label}最多可配置 200 项")
        for item in values:
            item_limit = 4000 if field.key in JSON_LINE_FIELD_KEYS else 300
            if len(item) > item_limit:
                raise ValueError(f"{field.label}中单项内容过长")
        if "regex" in field.key:
            for pattern in values:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"{field.label}包含无效正则：{exc}") from None
        return values

    if field.key in {"font_color", "bg_color"} and not COLOR_RE.fullmatch(value):
        raise ValueError(f"{field.label}应为 #RRGGBB 格式")
    if field.key in COMMAND_FIELD_KEYS:
        if not value or value.startswith("/"):
            raise ValueError(f"{field.label}只能填写不带斜杠和空格的命令名")
        raw_parts = re.split(r"[|,，、;；]+", value)
        if any(
            not part.strip()
            or len(part.strip()) > 32
            or any(character.isspace() for character in part.strip())
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in part)
            or part.strip().startswith("/")
            for part in raw_parts
        ):
            raise ValueError(
                f"{field.label}只能填写不带斜杠和空格的命令名，多个命令请用 | 分隔"
            )
        aliases = split_command_aliases(value, str(field.default or "指令"))
        if not aliases:
            raise ValueError(f"{field.label}至少需要一个命令名")
        return "|".join(aliases)
    if field.key == "database_profile" and value and not DATABASE_PROFILE_ID_RE.fullmatch(value):
        raise ValueError(f"{field.label}只能使用 1-48 位字母、数字、_ 或 -")
    if field.key == "database_path" and value:
        normalized_path = value.replace("\\", "/")
        path_parts = [part for part in normalized_path.split("/") if part not in {"", "."}]
        if (
            normalized_path.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized_path)
            or ".." in path_parts
            or "\x00" in normalized_path
        ):
            raise ValueError(f"{field.label}必须是 Windows 插件目录内的相对路径")
        return "/".join(path_parts)
    return value


def display_value(field: FieldSpec, value: Any) -> Any:
    if field.kind == "list":
        # The current projection always stores lists, but a historic hand-made
        # configuration may contain one string or another JSON scalar.  Keep
        # the settings page readable instead of letting Jinja fail while it
        # renders ``join`` for a non-iterable value.
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = []
        if field.key in JSON_LINE_FIELD_KEYS:
            return "\n".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                if isinstance(item, dict)
                else str(item)
                for item in items
            )
        return "\n".join(str(item) for item in items)
    return value

"""SMTP-backed email verification and administrator-managed delivery settings."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import timedelta
from email.headerregistry import Address
from email.message import EmailMessage
import hashlib
import hmac
import secrets
import smtplib
import ssl
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import EmailVerification, PortalSetting, User, utcnow
from .security import validate_email
from .services import PortalError
from .settings import Settings


EMAIL_SETTING_KEY = "email_delivery"
REGISTER_PURPOSE = "register"
PASSWORD_RESET_PURPOSE = "password_reset"
CODE_LENGTH = 6
CODE_TTL_MINUTES = 10
RESEND_INTERVAL_SECONDS = 60
MAX_SENDS_PER_EMAIL = 3
MAX_SENDS_PER_IP = 10
RATE_LIMIT_WINDOW_MINUTES = 15
MAX_VERIFY_ATTEMPTS = 5
TRANSPORTS = {"ssl", "starttls", "plain"}


class VerificationCodeError(PortalError):
    """An error that intentionally persists a verification attempt change."""


@dataclass(frozen=True)
class DeliveryConfig:
    enabled: bool
    host: str
    port: int
    transport: str
    username: str
    password: str
    sender_email: str
    sender_name: str
    code_ttl_minutes: int


def _fernet(settings: Settings) -> Fernet:
    # The session secret is generated during install and kept across upgrades.
    # Deriving a dedicated Fernet key means SMTP authorisation codes are not
    # stored as plaintext in MySQL or rendered back into the administrator UI.
    key = base64.urlsafe_b64encode(hashlib.sha256(
        settings.session_secret.encode("utf-8")
    ).digest())
    return Fernet(key)


def _encrypt(value: str, settings: Settings) -> str:
    return _fernet(settings).encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str, settings: Settings) -> str:
    try:
        return _fernet(settings).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise PortalError("已保存的邮箱密码无法读取，请在后台重新填写并保存") from exc


def _setting_payload(db: Session) -> dict[str, Any]:
    setting = db.get(PortalSetting, EMAIL_SETTING_KEY)
    return dict(setting.value_json) if setting and isinstance(setting.value_json, dict) else {}


def _safe_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(payload.get("enabled", False)),
        "host": str(payload.get("host", "")),
        "port": int(payload.get("port", 465) or 465),
        "transport": str(payload.get("transport", "ssl")),
        "username": str(payload.get("username", "")),
        "sender_email": str(payload.get("sender_email", "")),
        "sender_name": str(payload.get("sender_name", "Joker 群管客户中心")),
        # This address receives asynchronous new-feedback notifications.  It
        # is optional because an existing administrator email is used as a
        # safe fallback for older installations.
        "feedback_recipient": str(payload.get("feedback_recipient", "")),
        "code_ttl_minutes": int(payload.get("code_ttl_minutes", CODE_TTL_MINUTES) or CODE_TTL_MINUTES),
        "password_configured": bool(payload.get("password_ciphertext", "")),
    }


def get_email_config_view(db: Session) -> dict[str, Any]:
    """Return administrator-safe settings; never expose the SMTP password."""
    return _safe_view(_setting_payload(db))


def _clean_text(value: Any, limit: int, label: str) -> str:
    result = str(value or "").strip()
    if len(result) > limit or "\r" in result or "\n" in result:
        raise PortalError(f"{label}格式不正确")
    return result


def save_email_config(
    db: Session,
    settings: Settings,
    form: Mapping[str, Any],
    operator_id: int | None,
) -> dict[str, Any]:
    """Validate and persist generic SMTP settings for any mail provider."""
    previous = _setting_payload(db)
    enabled = str(form.get("enabled", "")).lower() in {"1", "true", "on", "yes"}
    host = _clean_text(form.get("host"), 253, "SMTP 服务器")
    username = _clean_text(form.get("username"), 320, "SMTP 用户名")
    sender_name = _clean_text(form.get("sender_name"), 80, "发件人名称")
    transport = str(form.get("transport", "ssl")).strip().lower()
    if transport not in TRANSPORTS:
        raise PortalError("请选择正确的 SMTP 加密方式")
    try:
        port = int(str(form.get("port", "465")))
        ttl = int(str(form.get("code_ttl_minutes", CODE_TTL_MINUTES)))
    except (TypeError, ValueError):
        raise PortalError("SMTP 端口和验证码有效期必须为数字") from None
    if not 1 <= port <= 65535:
        raise PortalError("SMTP 端口必须在 1-65535 之间")
    if not 5 <= ttl <= 30:
        raise PortalError("验证码有效期必须在 5-30 分钟之间")

    sender_raw = _clean_text(form.get("sender_email"), 320, "发件邮箱")
    sender_email = validate_email(sender_raw) if sender_raw else ""
    feedback_recipient_raw = _clean_text(
        form.get("feedback_recipient"), 320, "反馈通知邮箱"
    )
    feedback_recipient = (
        validate_email(feedback_recipient_raw) if feedback_recipient_raw else ""
    )
    supplied_password = str(form.get("password", ""))
    password_ciphertext = str(previous.get("password_ciphertext", ""))
    if supplied_password:
        if len(supplied_password) > 512 or "\r" in supplied_password or "\n" in supplied_password:
            raise PortalError("SMTP 密码或授权码格式不正确")
        password_ciphertext = _encrypt(supplied_password, settings)

    if enabled:
        if not host or "." not in host:
            raise PortalError("请填写正确的 SMTP 服务器地址，例如 smtp.qq.com")
        if not sender_email:
            raise PortalError("启用邮箱验证时必须填写发件邮箱")
        if not password_ciphertext:
            raise PortalError("启用邮箱验证时必须填写 SMTP 密码或授权码")

    payload = {
        "enabled": enabled,
        "host": host,
        "port": port,
        "transport": transport,
        "username": username,
        "sender_email": sender_email,
        "sender_name": sender_name or "Joker 群管客户中心",
        "feedback_recipient": feedback_recipient,
        "code_ttl_minutes": ttl,
        "password_ciphertext": password_ciphertext,
    }
    setting = db.get(PortalSetting, EMAIL_SETTING_KEY)
    if setting is None:
        setting = PortalSetting(key=EMAIL_SETTING_KEY, value_json=payload, updated_by=operator_id)
        db.add(setting)
    else:
        setting.value_json = payload
        setting.updated_by = operator_id
    return _safe_view(payload)


def _delivery_config(db: Session, settings: Settings) -> DeliveryConfig:
    payload = _setting_payload(db)
    view = _safe_view(payload)
    if not view["enabled"]:
        raise PortalError("邮箱验证码功能暂未开放，请联系管理员")
    password_ciphertext = str(payload.get("password_ciphertext", ""))
    if not password_ciphertext:
        raise PortalError("管理员尚未完成 SMTP 邮箱配置")
    return DeliveryConfig(
        enabled=True,
        host=view["host"],
        port=view["port"],
        transport=view["transport"],
        username=view["username"],
        password=_decrypt(password_ciphertext, settings),
        sender_email=view["sender_email"],
        sender_name=view["sender_name"],
        code_ttl_minutes=view["code_ttl_minutes"],
    )


def _send_mail(config: DeliveryConfig, recipient: str, subject: str, content: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = str(Address(config.sender_name, addr_spec=config.sender_email))
    message["To"] = recipient
    message.set_content(content)

    try:
        context = ssl.create_default_context()
        if config.transport == "ssl":
            client: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(
                config.host, config.port, timeout=15, context=context
            )
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=15)
        with client:
            client.ehlo()
            if config.transport == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if config.username:
                client.login(config.username, config.password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise PortalError("邮件发送失败，请检查 SMTP 服务器、端口、加密方式和授权码") from exc


def send_test_email(db: Session, settings: Settings, recipient: str) -> None:
    config = _delivery_config(db, settings)
    target = validate_email(recipient)
    _send_mail(
        config,
        target,
        "Joker 群管客户中心 - SMTP 测试成功",
        "这是一封 SMTP 测试邮件。收到此邮件说明邮箱验证码发送已配置成功。",
    )


def get_feedback_recipient(db: Session) -> str:
    """Return the configured support mailbox or an active admin fallback.

    The recipient is intentionally resolved only when a report is submitted.
    This keeps old deployments working without requiring an administrator to
    revisit SMTP settings immediately after upgrading.
    """

    configured = str(_setting_payload(db).get("feedback_recipient", "")).strip()
    if configured:
        try:
            return validate_email(configured)
        except ValueError:
            # Do not turn a historical hand-edited setting into a failed
            # feedback submission.  The administrator can correct it in the
            # email settings page; meanwhile use the ordinary admin fallback.
            pass
    fallback = db.scalar(
        select(User.email)
        .where(
            User.role == "admin",
            User.is_active.is_(True),
            User.email.is_not(None),
            User.email != "",
        )
        .order_by(User.id)
        .limit(1)
    )
    if not fallback:
        return ""
    try:
        return validate_email(str(fallback))
    except ValueError:
        return ""


def send_feedback_notification(
    db: Session,
    settings: Settings,
    recipient: str,
    *,
    feedback_id: int,
    username: str,
    group_id: str,
    category: str,
    subject: str,
    content: str,
) -> None:
    """Send a plain-text new-feedback notification to the support mailbox."""

    target = validate_email(recipient)
    config = _delivery_config(db, settings)
    group_text = group_id or "未指定群聊"
    _send_mail(
        config,
        target,
        f"Joker 群管客户中心 - 新用户反馈 #{feedback_id}",
        "收到一条新的用户反馈。\n\n"
        f"编号：#{feedback_id}\n"
        f"用户：{username}\n"
        f"群聊：{group_text}\n"
        f"分类：{category}\n"
        f"主题：{subject}\n\n"
        f"内容：\n{content}\n\n"
        "请登录 Joker 管理后台的“用户反馈”页面查看并处理。",
    )


def _purpose_details(purpose: str) -> tuple[str, str]:
    if purpose == REGISTER_PURPOSE:
        return "注册验证码", "用于创建 Joker 群管客户中心账号"
    if purpose == PASSWORD_RESET_PURPOSE:
        return "找回密码验证码", "用于重设 Joker 群管客户中心密码"
    raise PortalError("验证码用途无效")


def _code_digest(settings: Settings, purpose: str, email: str, code: str) -> str:
    payload = f"{purpose}|{email}|{code}".encode("utf-8")
    return hmac.new(settings.session_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _request_code(
    db: Session,
    settings: Settings,
    email: str,
    request_ip: str,
    purpose: str,
) -> str:
    """Send one purpose-bound, rate-limited code and store only its digest."""
    title, action = _purpose_details(purpose)
    now = utcnow()
    latest = db.scalar(
        select(EmailVerification)
        .where(
            EmailVerification.email == email,
            EmailVerification.purpose == purpose,
        )
        .order_by(EmailVerification.created_at.desc())
        .limit(1)
    )
    if latest and latest.created_at >= now - timedelta(seconds=RESEND_INTERVAL_SECONDS):
        raise PortalError("验证码已发送，请等待 60 秒后再试")

    since = now - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    email_count = db.scalar(
        select(func.count()).select_from(EmailVerification).where(
            EmailVerification.email == email,
            EmailVerification.purpose == purpose,
            EmailVerification.created_at >= since,
        )
    ) or 0
    ip_count = db.scalar(
        select(func.count()).select_from(EmailVerification).where(
            EmailVerification.request_ip == request_ip[:64],
            EmailVerification.purpose == purpose,
            EmailVerification.created_at >= since,
        )
    ) or 0
    if email_count >= MAX_SENDS_PER_EMAIL or ip_count >= MAX_SENDS_PER_IP:
        raise PortalError("验证码发送次数过多，请 15 分钟后再试")

    config = _delivery_config(db, settings)
    code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
    _send_mail(
        config,
        email,
        f"Joker 群管客户中心 - {title}",
        f"你的{title}是：{code}\n\n"
        f"该验证码{action}。\n"
        f"验证码 {config.code_ttl_minutes} 分钟内有效，请勿转发给他人。\n"
        "如果不是你本人操作，请忽略此邮件。",
    )
    db.add(
        EmailVerification(
            email=email,
            purpose=purpose,
            code_digest=_code_digest(settings, purpose, email, code),
            expires_at=now + timedelta(minutes=config.code_ttl_minutes),
            request_ip=request_ip[:64],
        )
    )
    return email


def request_registration_code(
    db: Session, settings: Settings, raw_email: str, request_ip: str
) -> str:
    """Send a code only when the email has not registered an account yet."""
    email = validate_email(raw_email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise PortalError("该邮箱已经注册，请直接登录或找回密码")
    return _request_code(db, settings, email, request_ip, REGISTER_PURPOSE)


def request_password_reset_code(
    db: Session, settings: Settings, raw_email: str, request_ip: str
) -> str:
    """Send a reset code only to a verified email of an active account."""
    email = validate_email(raw_email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        raise PortalError("该邮箱没有可找回的有效账号")
    return _request_code(db, settings, email, request_ip, PASSWORD_RESET_PURPOSE)


def _consume_code(
    db: Session,
    settings: Settings,
    raw_email: str,
    raw_code: str,
    purpose: str,
) -> str:
    """Consume the newest purpose-bound code and retain failed attempts."""
    _purpose_details(purpose)
    email = validate_email(raw_email)
    record = db.scalar(
        select(EmailVerification)
        .where(
            EmailVerification.email == email,
            EmailVerification.purpose == purpose,
            EmailVerification.consumed_at.is_(None),
        )
        .order_by(EmailVerification.created_at.desc())
        .limit(1)
    )
    now = utcnow()
    if record is None:
        raise VerificationCodeError("请先获取邮箱验证码")
    if record.expires_at <= now:
        record.consumed_at = now
        raise VerificationCodeError("验证码已过期，请重新获取")
    if record.attempts >= MAX_VERIFY_ATTEMPTS:
        record.consumed_at = now
        raise VerificationCodeError("验证码错误次数过多，请重新获取")

    code = str(raw_code or "").strip()
    expected = _code_digest(settings, purpose, email, code)
    if not hmac.compare_digest(record.code_digest, expected):
        record.attempts += 1
        if record.attempts >= MAX_VERIFY_ATTEMPTS:
            record.consumed_at = now
            raise VerificationCodeError("验证码错误次数过多，请重新获取")
        raise VerificationCodeError("验证码错误，请重新输入")
    record.consumed_at = now
    return email


def consume_registration_code(
    db: Session, settings: Settings, raw_email: str, raw_code: str
) -> str:
    return _consume_code(db, settings, raw_email, raw_code, REGISTER_PURPOSE)


def consume_password_reset_code(
    db: Session, settings: Settings, raw_email: str, raw_code: str
) -> str:
    return _consume_code(db, settings, raw_email, raw_code, PASSWORD_RESET_PURPOSE)

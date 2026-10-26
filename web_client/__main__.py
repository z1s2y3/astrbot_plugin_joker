"""Run with: python -m web_client"""

import argparse
import getpass
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


if __name__ == "__main__":
    # Uvicorn's bind address is evaluated before the application factory loads
    # Settings, so load the same .env here as well. Otherwise custom
    # JOKER_WEB_HOST/JOKER_WEB_PORT values would be silently ignored.
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    # AstrBotLauncher keeps plugins several directories below the AstrBot
    # source tree. Add that tree when the portal is started from this folder.
    for parent in Path(__file__).resolve().parents:
        if (parent / "astrbot").is_dir():
            sys.path.insert(0, str(parent))
            break
    parser = argparse.ArgumentParser(description="Joker customer portal")
    subcommands = parser.add_subparsers(dest="command")
    create_admin = subcommands.add_parser("create-admin", help="create or reset an admin")
    create_admin.add_argument("username", help="administrator username")
    create_admin.add_argument(
        "--password-env",
        action="store_true",
        help="read the one-time password from JOKER_WEB_BOOTSTRAP_ADMIN_PASSWORD",
    )
    args = parser.parse_args()

    if args.command == "create-admin":
        from .database import get_session_factory, init_database
        from .services import create_admin_account

        if args.password_env:
            password = os.getenv("JOKER_WEB_BOOTSTRAP_ADMIN_PASSWORD", "")
            if not password:
                raise SystemExit("未设置一次性管理员密码环境变量")
        else:
            password = getpass.getpass("管理员密码（至少 8 位）: ")
            confirmation = getpass.getpass("再次输入密码: ")
            if password != confirmation:
                raise SystemExit("两次输入的密码不一致")
        init_database()
        with get_session_factory()() as session:
            user = create_admin_account(session, args.username, password)
        print(f"管理员 {user.username} 已创建或更新")
    else:
        uvicorn.run(
            "web_client.app:create_app",
            factory=True,
            host=os.getenv("JOKER_WEB_HOST", "127.0.0.1"),
            port=int(os.getenv("JOKER_WEB_PORT", "8765")),
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
            timeout_keep_alive=15,
            timeout_graceful_shutdown=10,
        )

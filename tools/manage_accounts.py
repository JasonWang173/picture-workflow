"""Create and maintain accounts from the service host's terminal."""
from __future__ import annotations

import argparse
import sys
from getpass import getpass
from sqlite3 import Error as SQLiteError

from food_compositor.accounts import AccountStore, accounts_path


def read_password() -> str:
    if not sys.stdin.isatty():
        raise ValueError("请在交互式终端输入密码")
    password = getpass("密码：")
    if password != getpass("再次输入密码："):
        raise ValueError("两次密码不一致")
    return password


def main() -> int:
    parser = argparse.ArgumentParser(description="管理图片工作台账号")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="创建账号")
    create.add_argument("username")
    create.add_argument("--display-name", default="", help="工作台显示的名字")
    commands.add_parser("list", help="查看账号")
    for name, description in (("reset-password", "重设密码"), ("disable", "停用账号"), ("enable", "启用账号")):
        command = commands.add_parser(name, help=description)
        command.add_argument("username")
    args = parser.parse_args()
    store = AccountStore(accounts_path())
    try:
        if args.command == "list":
            for account in store.list_accounts():
                status = "已启用" if account["enabled"] else "已停用"
                print(f"{account['username']}\t{account['display_name']}\t{status}")
            return 0
        if args.command == "create":
            store.create(args.username, args.display_name, read_password())
        elif args.command == "reset-password":
            store.reset_password(args.username, read_password())
        else:
            store.set_enabled(args.username, args.command == "enable")
    except (ValueError, SQLiteError, OSError) as exc:
        print(f"账号操作失败：{exc}", file=sys.stderr)
        return 1
    print("账号已更新。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

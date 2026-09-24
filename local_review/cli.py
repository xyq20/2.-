from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

from .config import Settings
from .database import backup_if_due, connect, migrate, transaction
from .security import create_user


def _user_count(settings: Settings) -> int:
    migrate(settings)
    with connect(settings) as connection:
        return int(connection.execute("SELECT count(*) FROM users").fetchone()[0])


def _init_admin(settings: Settings, username: str) -> int:
    if _user_count(settings):
        print("审核账号已经存在，无需重复初始化。")
        return 0
    password = getpass.getpass("请设置 4 位数字审核密码: ")
    repeated = getpass.getpass("请再次输入管理员密码: ")
    if password != repeated:
        print("两次密码不一致，未创建账号。", file=sys.stderr)
        return 2
    try:
        with transaction(settings) as connection:
            create_user(connection, username, password, role="admin")
    except (ValueError, sqlite3.IntegrityError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"管理员账号 {username} 已创建；数据库中只保存加盐哈希。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="快麦本地审核中心管理命令")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("has-users", help="检查是否已创建审核账号")
    initialize = subparsers.add_parser("init-admin", help="交互创建首个管理员")
    initialize.add_argument("--username", default="admin")
    backup = subparsers.add_parser("backup", help="备份本地审核数据库")
    backup.add_argument("--force", action="store_true")
    subparsers.add_parser("status", help="显示本地数据路径和记录数")
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "has-users":
        return 0 if _user_count(settings) else 1
    if args.command == "init-admin":
        return _init_admin(settings, args.username)
    if args.command == "backup":
        destination = backup_if_due(settings, force=args.force)
        print(destination or "今天已有备份，无需重复生成。")
        return 0
    migrate(settings)
    with connect(settings) as connection:
        counts = {
            name: int(connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
            for name in ("products", "review_tasks", "persisted_readbacks")
        }
    print(f"数据库: {settings.database_path}")
    print(f"商品: {counts['products']}，待审任务: {counts['review_tasks']}，回读记录: {counts['persisted_readbacks']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

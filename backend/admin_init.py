#!/usr/bin/env python3
"""创建首个超管账号 (首次部署用)。

用法:
    docker exec kefu-backend python admin_init.py <username> <password>

或在容器外:
    python admin_init.py <username> <password>

不会覆盖已存在的同名账号。
"""
import sys

import bcrypt

from app.storage import get_storage


def main():
    if len(sys.argv) != 3:
        print(f"用法: python {sys.argv[0]} <username> <password>")
        sys.exit(1)
    username, password = sys.argv[1].strip(), sys.argv[2]
    if not username or not password:
        print("用户名和密码不能为空")
        sys.exit(1)
    if len(password) < 6:
        print("密码至少 6 位")
        sys.exit(1)

    storage = get_storage()
    existing = storage.conn.execute(
        "SELECT 1 FROM admins WHERE username = ?", (username,)
    ).fetchone()
    if existing:
        print(f"用户 {username} 已存在,跳过")
        sys.exit(0)

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    storage.conn.execute(
        "INSERT INTO admins (username, password_hash, role, created_by) VALUES (?, ?, ?, ?)",
        (username, hashed, "super", "init-script"),
    )
    print(f"✓ 超管账号 {username} 创建成功")


if __name__ == "__main__":
    main()

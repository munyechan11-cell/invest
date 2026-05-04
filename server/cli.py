"""Sift 운영용 CLI.

사용:
    python -m server.cli create-admin <username> <password>
    python -m server.cli reset-password <username> <new_password>
    python -m server.cli list-users
    python -m server.cli delete-user <username>

주의:
    - 비밀번호는 채팅/이슈/코드에 절대 평문으로 남기지 말 것.
    - 가능하면 환경변수 ADMIN_USERNAME / ADMIN_PASSWORD 로 운영.
    - CLI는 로컬 1회성 부트스트랩 용도.
"""
from __future__ import annotations
import sys
import asyncio
import time

# Windows cp949 콘솔에서 한글/이모지 출력 안전화
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from server import db


async def create_admin(username: str, password: str):
    if len(password) < 8:
        print(f"❌ 비밀번호는 8자 이상이어야 합니다 (현재 {len(password)}자)")
        return False

    await db.init()
    c = await db.get_db()
    pw_hash = db._hash(password)

    row = await (await c.execute("SELECT id FROM users WHERE username=?", (username,))).fetchone()
    if row:
        # 기존 계정 → 관리자 권한 + 비번 갱신
        await c.execute(
            "UPDATE users SET pw_hash=?, is_admin=1 WHERE username=?",
            (pw_hash, username),
        )
        await c.commit()
        print(f"✅ 기존 계정 '{username}' → 관리자로 승격 + 비번 갱신")
    else:
        await c.execute(
            "INSERT INTO users(username, display_name, pw_hash, is_admin, created_at) "
            "VALUES(?,?,?,1,?)",
            (username, "마스터 관리자", pw_hash, time.time()),
        )
        await c.commit()
        print(f"✅ 마스터 계정 생성: '{username}'")
    print(f"   로그인: 사이트 → 아이디 {username} / 비번 (입력하신 것)")
    return True


async def reset_password(username: str, new_password: str):
    if len(new_password) < 8:
        print(f"❌ 비밀번호 8자 이상")
        return False
    await db.init()
    c = await db.get_db()
    row = await (await c.execute("SELECT id FROM users WHERE username=?", (username,))).fetchone()
    if not row:
        print(f"❌ 계정 없음: {username}")
        return False
    await c.execute(
        "UPDATE users SET pw_hash=? WHERE username=?",
        (db._hash(new_password), username),
    )
    await c.commit()
    print(f"✅ '{username}' 비밀번호 갱신")
    return True


async def list_users():
    await db.init()
    users = await db.list_users()
    print(f"=== 등록 사용자 {len(users)}명 ===")
    for u in users:
        admin = " [ADMIN]" if u.get("is_admin") else ""
        print(f"  #{u['id']:3} {u['username']:20} {u['display_name'] or '-':15}{admin}")


async def delete_user(username: str):
    await db.init()
    c = await db.get_db()
    row = await (await c.execute(
        "SELECT id, is_admin FROM users WHERE username=?", (username,)
    )).fetchone()
    if not row:
        print(f"❌ 계정 없음: {username}")
        return
    if dict(row).get("is_admin"):
        print(f"⚠️ '{username}'은 관리자 — 삭제 거부 (먼저 권한 회수 필요)")
        return
    await c.execute("DELETE FROM users WHERE username=?", (username,))
    await c.commit()
    print(f"✅ '{username}' 삭제됨")


def usage():
    print(__doc__)


def main():
    if len(sys.argv) < 2:
        usage()
        return
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd == "create-admin":
        if len(args) != 2:
            print("사용: python -m server.cli create-admin <username> <password>")
            return
        asyncio.run(create_admin(args[0], args[1]))
    elif cmd == "reset-password":
        if len(args) != 2:
            print("사용: python -m server.cli reset-password <username> <new_password>")
            return
        asyncio.run(reset_password(args[0], args[1]))
    elif cmd == "list-users":
        asyncio.run(list_users())
    elif cmd == "delete-user":
        if len(args) != 1:
            print("사용: python -m server.cli delete-user <username>")
            return
        asyncio.run(delete_user(args[0]))
    else:
        usage()


if __name__ == "__main__":
    main()

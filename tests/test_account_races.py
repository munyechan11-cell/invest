"""동시에 들어오는 두 요청.

가입과 로그인은 비밀번호를 60만 번 늘이느라 느립니다. 그래서 이 두 경로는
`run_in_threadpool` 로 나가 있고, 그 말은 **사람 둘이 같은 순간에 가입하면
같은 sqlite 연결 위에서 두 스레드가 동시에 움직인다**는 뜻입니다. 이건
이론적인 경합이 아니라 서비스를 여는 첫날 일어나는 일입니다.
"""
from __future__ import annotations

import threading

import pytest

from quant.webapp.accounts import Accounts

GOOD = "correct-horse-9"


@pytest.fixture
def accounts(tmp_path):
    return Accounts(tmp_path / "a.db", secret="x" * 40)


def test_only_the_very_first_registration_is_an_admin(accounts):
    """세는 것과 넣는 것이 갈라져 있으면 둘 다 관리자가 됩니다.

    관리자는 다른 사람의 사용량과 감사 기록을 봅니다. 가입 순간의 경합으로
    남이 관리자가 되는 것은, 늦게 고쳐도 되는 종류의 버그가 아닙니다.
    """
    made, errors = [], []
    ready = threading.Barrier(8)

    def join(i: int) -> None:
        ready.wait()
        try:
            made.append(accounts.register(f"u{i}@example.com", GOOD))
        except Exception as exc:            # noqa: BLE001 — 무엇이 터졌든 보고합니다
            errors.append(exc)

    threads = [threading.Thread(target=join, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"동시 가입이 터졌습니다: {errors}"
    assert len(made) == 8
    admins = [u for u in made if u.is_admin]
    assert len(admins) == 1, f"관리자가 {len(admins)}명 생겼습니다"


def test_reads_and_writes_can_run_at_the_same_time(accounts):
    """읽기를 잠금 밖에 두면 3.9 의 sqlite3 는 프로세스를 죽입니다.

    커서가 게을러서, `execute()` 가 돌아온 뒤 `fetchone()` 을 부를 때 엔진이
    움직입니다. 그 사이에 다른 스레드가 같은 연결로 INSERT 를 커밋하면
    segfault 입니다 — 예외가 아니라 프로세스 종료라, 잡을 수도 없습니다.
    """
    for i in range(20):
        accounts.register(f"seed{i}@example.com", GOOD)

    errors = []
    stop = threading.Event()

    def read() -> None:
        try:
            while not stop.is_set():
                accounts.count()
                accounts.by_email("seed3@example.com")
        except Exception as exc:            # noqa: BLE001
            errors.append(exc)

    readers = [threading.Thread(target=read) for _ in range(4)]
    for r in readers:
        r.start()
    try:
        for i in range(40):
            accounts.register(f"w{i}@example.com", GOOD)
    finally:
        stop.set()
        for r in readers:
            r.join()

    assert not errors, f"동시 읽기/쓰기가 터졌습니다: {errors}"
    assert accounts.count() == 60

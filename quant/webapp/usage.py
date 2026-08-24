"""사용자별 AI 데스크 사용량과 요금제.

데스크 비용은 서비스 운영자가 냅니다 — 사용자는 자기 Gemini 키 없이도 바로
써볼 수 있고, 그게 이 서비스를 처음 여는 사람에게 가장 큰 차이입니다.
대신 두 가지가 따라옵니다.

**상한이 있어야 합니다.** 심의 한 번이 약 $0.06 입니다. 일봉 3종목이면 한 사람이
월 $5 지만, 같은 사람이 5분봉으로 돌리면 하루 78봉 × 3종목 = 월 $400 입니다.
요금제를 아직 안 만들었더라도 상한은 있어야 합니다. 이건 과금 정책이 아니라
폭주 방지입니다 — 한 사람의 설정 실수가 운영자 카드로 청구되지 않게 하는 것.

**지금부터 재야 합니다.** 요금제는 나중 일이지만, 나중에 붙일 때 "누가 얼마나
썼는가"를 소급해서 만들 수는 없습니다. 그래서 첫날부터 사용자·날짜별로
호출 수와 토큰과 비용을 남깁니다. 요금제가 생기면 그때는 `plan` 값만 바꾸면
됩니다 — 계량은 이미 돌고 있습니다.

한도에 걸려도 자동매매가 멈추지는 않습니다. 데스크만 꺼지고 규칙 기반 알파는
계속 돕니다. 그쪽은 비용이 0원이라 무료 사용자에게도 줄 수 있습니다.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from quant.alpha.llm_client import price_for
from quant.core.types import UTC
from quant.webapp.accounts import make_db_private

log = logging.getLogger("quant.usage")

#: 하루 경계는 한국 장 시간을 따릅니다. UTC 자정에 초기화하면 장중에 리셋됩니다.
_KST = timedelta(hours=9)


@dataclass(frozen=True)
class Plan:
    """요금제 하나. 지금은 free 만 쓰지만 자리는 만들어 둡니다."""

    id: str
    label_ko: str
    #: 하루 심의 횟수. 0 이면 데스크를 못 씁니다(규칙 기반 알파는 그대로).
    daily_deliberations: int
    #: 한 달 비용 상한(USD). 하루 상한을 통과해도 여기서 막힙니다.
    monthly_cost_usd: float
    #: 자기 키를 넣으면 위 두 상한을 받지 않습니다.
    byo_key_unlimited: bool = True

    @property
    def unlimited(self) -> bool:
        return self.daily_deliberations <= 0 and self.monthly_cost_usd <= 0


#: 지금은 전원 free 입니다. 요금제를 열 때 여기에 줄을 더하고 users.plan 만
#: 바꾸면 되며, 계량과 집행 코드는 손대지 않습니다.
PLANS: dict[str, Plan] = {
    "free": Plan("free", "무료", daily_deliberations=5, monthly_cost_usd=3.0),
    "basic": Plan("basic", "베이직", daily_deliberations=40, monthly_cost_usd=25.0),
    "pro": Plan("pro", "프로", daily_deliberations=200, monthly_cost_usd=120.0),
    "unlimited": Plan("unlimited", "무제한", daily_deliberations=0, monthly_cost_usd=0.0),
}
DEFAULT_PLAN = "free"


def plan_for(name: str | None) -> Plan:
    return PLANS.get((name or "").strip() or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def _kst_day(now: datetime | None = None) -> date:
    """그 시각이 속한 한국 날짜.

    tz 를 달고 온 시각은 UTC 로 맞춘 뒤에 더합니다. 이미 KST 인 시각에 9시간을
    또 더하면 하루 경계가 오후 3시로 밀려서, 이 모듈이 UTC 자정을 피하려던
    이유 그대로 장 한복판에서 리셋됩니다. tz 가 없으면 UTC 로 봅니다.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is not None:
        now = now.astimezone(UTC)
    return (now + _KST).date()


class UsageStore:
    """사용자·날짜별 데스크 사용량, 그리고 그 위에 얹힌 상한."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # 누가 무엇을 얼마나 쓰는지도 그 사람 것입니다. 계정 DB 와 같은 이유로
        # WAL 사이드카까지 함께 잠급니다.
        make_db_private(self.path)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS desk_usage (
                user_id        INTEGER NOT NULL,
                day            TEXT NOT NULL,
                deliberations  INTEGER NOT NULL DEFAULT 0,
                llm_calls      INTEGER NOT NULL DEFAULT 0,
                input_tokens   INTEGER NOT NULL DEFAULT 0,
                output_tokens  INTEGER NOT NULL DEFAULT 0,
                cost_usd       REAL NOT NULL DEFAULT 0.0,
                own_key        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day, own_key)
            );
            CREATE INDEX IF NOT EXISTS desk_usage_day ON desk_usage(day);
            """
        )
        self.conn.commit()

    # ── 기록 ─────────────────────────────────────────────────────────────
    def record(self, user_id: int, *, model: str, llm_calls: int,
               input_tokens: int, output_tokens: int,
               own_key: bool = False, now: datetime | None = None) -> float:
        """심의 한 번을 기록하고 그 비용을 돌려줍니다.

        `own_key` 는 사용자가 자기 Gemini 키로 돌린 경우입니다. 그때도 기록은
        남기지만 — 얼마나 쓰는지는 알아야 하니까 — 운영자 비용에는 넣지 않고
        상한도 적용하지 않습니다.
        """
        pin, pout = price_for(model)
        cost = input_tokens / 1e6 * pin + output_tokens / 1e6 * pout
        with self._lock:
            self.conn.execute(
                "INSERT INTO desk_usage (user_id, day, deliberations, llm_calls, "
                "input_tokens, output_tokens, cost_usd, own_key) "
                "VALUES (?,?,1,?,?,?,?,?) "
                "ON CONFLICT(user_id, day, own_key) DO UPDATE SET "
                "  deliberations = deliberations + 1, "
                "  llm_calls     = llm_calls + excluded.llm_calls, "
                "  input_tokens  = input_tokens + excluded.input_tokens, "
                "  output_tokens = output_tokens + excluded.output_tokens, "
                "  cost_usd      = cost_usd + excluded.cost_usd",
                (user_id, _kst_day(now).isoformat(), llm_calls, input_tokens,
                 output_tokens, cost, 1 if own_key else 0))
            self.conn.commit()
        return cost

    def record_spend(self, user_id: int, llm_calls: int, cost_usd: float,
                     own_key: bool = False, now: datetime | None = None) -> None:
        """이미 계산된 비용을 그대로 적습니다.

        `record` 는 모델 이름과 토큰 수로 비용을 다시 계산합니다. 그건 호출이
        한 모델로만 나갈 때 맞고, 데스크는 분석석과 결정석에 **다른 모델**을
        씁니다 — 하나의 이름으로 뭉쳐서 계산하면 싼 쪽을 비싸게(또는 그 반대로)
        치게 됩니다. 데스크는 자기 비용을 모델별로 이미 알고 있으므로, 그
        숫자를 받아 적는 편이 정확합니다.

        토큰 수는 남기지 않습니다 — 모델이 섞여 있어서 합계가 의미를 잃습니다.
        """
        with self._lock:
            self.conn.execute(
                "INSERT INTO desk_usage (user_id, day, deliberations, llm_calls, "
                "input_tokens, output_tokens, cost_usd, own_key) "
                "VALUES (?,?,1,?,0,0,?,?) "
                "ON CONFLICT(user_id, day, own_key) DO UPDATE SET "
                "  deliberations = deliberations + 1, "
                "  llm_calls     = llm_calls + excluded.llm_calls, "
                "  cost_usd      = cost_usd + excluded.cost_usd",
                (user_id, _kst_day(now).isoformat(), llm_calls,
                 float(cost_usd), 1 if own_key else 0))
            self.conn.commit()

    # ── 조회 ─────────────────────────────────────────────────────────────
    def today(self, user_id: int, now: datetime | None = None) -> dict:
        return self._sum("user_id=? AND day=? AND own_key=0",
                         (user_id, _kst_day(now).isoformat()))

    def month(self, user_id: int, now: datetime | None = None) -> dict:
        first = _kst_day(now).replace(day=1).isoformat()
        return self._sum("user_id=? AND day>=? AND own_key=0", (user_id, first))

    def operator_month(self, now: datetime | None = None) -> dict:
        """운영자가 이번 달 전체 사용자에게 쓴 비용."""
        first = _kst_day(now).replace(day=1).isoformat()
        return self._sum("day>=? AND own_key=0", (first,))

    def _sum(self, where: str, args: tuple) -> dict:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(deliberations),0) d, COALESCE(SUM(llm_calls),0) c, "
            "COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
            f"COALESCE(SUM(cost_usd),0.0) u FROM desk_usage WHERE {where}", args).fetchone()
        return {"deliberations": row["d"], "llm_calls": row["c"],
                "input_tokens": row["i"], "output_tokens": row["o"],
                "cost_usd": round(row["u"], 4)}

    def leaderboard(self, limit: int = 20, now: datetime | None = None) -> list[dict]:
        """이번 달 누가 얼마나 쓰는가 — 요금제를 정할 때 볼 숫자."""
        first = _kst_day(now).replace(day=1).isoformat()
        rows = self.conn.execute(
            "SELECT user_id, SUM(deliberations) d, SUM(cost_usd) u FROM desk_usage "
            "WHERE day>=? AND own_key=0 GROUP BY user_id ORDER BY u DESC LIMIT ?",
            (first, limit)).fetchall()
        return [{"user_id": r["user_id"], "deliberations": r["d"],
                 "cost_usd": round(r["u"], 4)} for r in rows]

    # ── 집행 ─────────────────────────────────────────────────────────────
    def allow(self, user_id: int, plan_name: str | None,
              own_key: bool = False, now: datetime | None = None) -> tuple[bool, str]:
        """(허용, 사유). 사유는 사용자에게 그대로 보여줄 수 있는 문장입니다."""
        plan = plan_for(plan_name)
        if own_key and plan.byo_key_unlimited:
            return True, ""
        if plan.unlimited:
            return True, ""

        if plan.daily_deliberations:
            used = self.today(user_id, now)["deliberations"]
            if used >= plan.daily_deliberations:
                return False, (
                    f"오늘 AI 데스크 심의 {used}/{plan.daily_deliberations}회를 "
                    f"모두 썼습니다 ({plan.label_ko} 요금제). 내일 다시 열리고, "
                    f"마이페이지에 본인 Gemini 키를 넣으면 제한 없이 쓸 수 있습니다. "
                    f"규칙 기반 전략은 그대로 돌아갑니다.")

        if plan.monthly_cost_usd:
            spent = self.month(user_id, now)["cost_usd"]
            if spent >= plan.monthly_cost_usd:
                return False, (
                    f"이번 달 AI 데스크 사용량 상한에 도달했습니다 "
                    f"({plan.label_ko} 요금제). 본인 Gemini 키를 넣으면 계속 쓸 수 "
                    f"있고, 규칙 기반 전략은 그대로 돌아갑니다.")
        return True, ""

    def status(self, user_id: int, plan_name: str | None, own_key: bool = False,
               now: datetime | None = None) -> dict:
        """마이페이지가 보여줄 것.

        `own_key` 는 집행(`allow`)이 받는 것과 같은 값을 받아야 합니다. 자기
        Gemini 키를 이미 넣은 사람에게 "상한에 걸렸으니 자기 키를 넣으라"고
        말하는 화면은, 틀린 데다 고칠 방법도 알려주지 못합니다.
        """
        plan = plan_for(plan_name)
        today, month = self.today(user_id, now), self.month(user_id, now)
        allowed, reason = self.allow(user_id, plan_name, own_key=own_key, now=now)
        return {
            "plan": {"id": plan.id, "label": plan.label_ko,
                     "daily_deliberations": plan.daily_deliberations or None,
                     "monthly_cost_usd": plan.monthly_cost_usd or None},
            "today": {"deliberations": today["deliberations"],
                      "limit": plan.daily_deliberations or None},
            "month": {"deliberations": month["deliberations"],
                      "cost_usd": month["cost_usd"],
                      "limit_usd": plan.monthly_cost_usd or None},
            "allowed": allowed,
            "reason": reason,
        }

    def close(self) -> None:
        self.conn.close()

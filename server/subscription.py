"""구독·쿠폰 도메인 로직.

핵심 함수:
- get_status(user_id): 사용자 현재 플랜 + 만료 + 할인 정보를 한 번에 계산
- is_pro(user_id): Pro 활성 여부 (trial 포함)
- apply_coupon(user_id, code): 쿠폰 적용 → 구독 활성화/연장
- require_pro: FastAPI 의존성 — 미구독자 차단

가격 정책 (한 곳에서 관리):
- Pro 월 9,900원, 연 79,000원 (33% 할인)
- 쿠폰 적용 시 discount_percent → 결제 시 차감
- trial_days 동안은 status='trial', 결제 발생 X
- duration_months 끝나면 정상가 자동 전환
"""
from __future__ import annotations
import time
import logging

from fastapi import HTTPException

from server import db

log = logging.getLogger("subscription")

PRICE_MONTHLY_KRW = 9_900
PRICE_YEARLY_KRW = 79_000

# 무료 한도
FREE_WATCHLIST_MAX = 5
FREE_ANALYSIS_PER_DAY = 3
FREE_OCR_PER_MONTH = 1


async def get_status(user_id: int) -> dict:
    """사용자 구독 상태 종합 — 만료 자동 정리 포함.

    관리자(is_admin=1)는 무조건 Pro 권한 (만료/결제 무관).
    """
    sub = await db.get_subscription(user_id)
    now = time.time()

    # 관리자 자동 Pro — DB users.is_admin 체크
    user_row = await db.get_user_by_id(user_id)
    is_admin = bool(user_row and user_row.get("is_admin"))

    raw_plan = sub.get("plan") or "free"
    status = sub.get("status") or "active"
    expires_at = sub.get("expires_at")
    trial_ends_at = sub.get("trial_ends_at")
    discount_pct = int(sub.get("discount_percent") or 0)
    discount_until = sub.get("discount_until")

    # 할인 만료 정리
    if discount_until and discount_until < now:
        discount_pct = 0
        await db.upsert_subscription(user_id, discount_percent=0, discount_until=None)

    # Trial 중인지 확인
    in_trial = bool(trial_ends_at and trial_ends_at > now)

    # 권한 판정 — 관리자 우선
    if is_admin:
        effective_plan = "pro"
        status = "admin"  # UI 에서 'PRO' 가 아닌 'ADMIN' 배지 가능
    elif raw_plan == "pro":
        if expires_at and expires_at < now and not in_trial:
            # 만료됨 → free 로 강등
            await db.upsert_subscription(user_id, plan="free", status="expired")
            effective_plan = "free"
        else:
            effective_plan = "pro"
    else:
        effective_plan = "free"

    base_price = PRICE_MONTHLY_KRW
    effective_price = int(base_price * (100 - discount_pct) / 100)

    return {
        "plan": effective_plan,
        "raw_plan": raw_plan,
        "status": "trial" if in_trial else status,
        "in_trial": in_trial,
        "is_admin": is_admin,
        "expires_at": expires_at,
        "trial_ends_at": trial_ends_at,
        "discount_percent": discount_pct,
        "discount_until": discount_until,
        "applied_coupon_code": sub.get("applied_coupon_code"),
        "base_price_krw": base_price,
        "effective_price_krw": effective_price,
    }


async def grant_pro(user_id: int, months: int | None = None) -> dict:
    """관리자가 사용자에게 Pro 부여. months=None 이면 평생(100년) Pro.

    기존 구독 만료일 무시하고 새로 설정. applied_coupon_code='ADMIN_GRANT'
    로 표시해서 추적 가능.
    """
    if months is None:
        # 평생 (100년)
        expires_at = time.time() + 100 * 365 * 86400
        label = "ADMIN_LIFETIME"
    else:
        expires_at = time.time() + int(months) * 30 * 86400
        label = f"ADMIN_{months}M"
    await db.upsert_subscription(
        user_id,
        plan="pro",
        status="active",
        expires_at=expires_at,
        trial_ends_at=None,
        applied_coupon_code=label,
        discount_percent=0,
        discount_until=None,
    )
    log.info(f"admin granted pro: user={user_id} months={months} label={label}")
    return await get_status(user_id)


async def revoke_pro(user_id: int) -> dict:
    """관리자가 부여한 Pro 회수 — Free 로 강등."""
    await db.upsert_subscription(
        user_id,
        plan="free",
        status="cancelled",
        expires_at=None,
        trial_ends_at=None,
        applied_coupon_code=None,
        discount_percent=0,
        discount_until=None,
    )
    log.info(f"admin revoked pro: user={user_id}")
    return await get_status(user_id)


async def is_pro(user_id: int) -> bool:
    s = await get_status(user_id)
    return s["plan"] == "pro"


async def apply_coupon(user_id: int, code: str) -> dict:
    """쿠폰 적용. 성공 시 갱신된 구독 상태 반환.

    오류는 ValueError로 raise (호출자가 HTTP 400/404 변환).
    """
    code = (code or "").strip().upper()
    if not code:
        raise ValueError("코드를 입력해주세요")

    coupon = await db.get_coupon_by_code(code)
    if not coupon:
        raise ValueError("유효하지 않은 코드")
    if not coupon.get("active"):
        raise ValueError("비활성화된 코드")
    if coupon.get("expires_at") and coupon["expires_at"] < time.time():
        raise ValueError("만료된 코드")
    max_uses = coupon.get("max_uses")
    if max_uses is not None and coupon.get("used_count", 0) >= max_uses:
        raise ValueError("사용 한도가 모두 소진된 코드")
    if await db.has_redeemed(coupon["id"], user_id):
        raise ValueError("이미 적용한 코드")

    now = time.time()
    discount_pct = int(coupon.get("discount_percent") or 0)
    trial_days = int(coupon.get("trial_days") or 0)
    duration_months = coupon.get("duration_months")

    # 1) Pro 활성화 — 100% 할인이면 expires_at = now + duration_months × 30일
    if discount_pct >= 100:
        # 완전 무료 기간 — duration_months 동안 Pro
        months = duration_months or 1
        expires_at = now + months * 30 * 86400
        await db.upsert_subscription(
            user_id,
            plan="pro",
            status="active",
            expires_at=expires_at,
            applied_coupon_code=code,
            discount_percent=0,
            discount_until=None,
        )
    else:
        # 부분 할인 — trial_days 동안 무료, 그 후 매월 결제 (할인 적용)
        # Phase 1에선 결제 없으므로 trial_days 동안만 Pro 활성화
        # Phase 2에서 결제 webhook 통해 정식 구독 전환
        trial_ends_at = now + trial_days * 86400 if trial_days > 0 else None
        # 할인 적용 기간
        discount_until = (
            now + duration_months * 30 * 86400
            if duration_months else None
        )
        # Trial 기간이 있으면 그 동안 Pro 권한 부여
        sub_fields = {
            "applied_coupon_code": code,
            "discount_percent": discount_pct,
            "discount_until": discount_until,
        }
        if trial_ends_at:
            sub_fields["plan"] = "pro"
            sub_fields["status"] = "trial"
            sub_fields["trial_ends_at"] = trial_ends_at
            sub_fields["expires_at"] = trial_ends_at  # trial 끝나면 자동 만료 (결제 전)
        await db.upsert_subscription(user_id, **sub_fields)

    # 2) 사용 기록
    await db.redeem_coupon(coupon["id"], user_id)
    log.info(f"coupon redeemed: user={user_id} code={code} pct={discount_pct} trial={trial_days}d")

    return {
        "ok": True,
        "code": code,
        "description": coupon.get("description"),
        "discount_percent": discount_pct,
        "trial_days": trial_days,
        "duration_months": duration_months,
        "status": await get_status(user_id),
    }


# ── FastAPI 의존성 ───────────────────────────────────────────
async def require_pro_dep(user_id: int) -> dict:
    """get_current_user 가 반환한 user["id"]로 호출.

    Pro 가 아니면 402 Payment Required.
    """
    status = await get_status(user_id)
    if status["plan"] != "pro":
        raise HTTPException(
            status_code=402,
            detail={
                "error": "pro_required",
                "message": "Pro 플랜이 필요한 기능입니다",
                "current_plan": status["plan"],
            },
        )
    return status


# ── 무료 한도 체크 ───────────────────────────────────────────
async def check_watchlist_quota(user_id: int) -> tuple[bool, int, int]:
    """워치리스트 추가 가능 여부. (allowed, current, limit) 반환."""
    if await is_pro(user_id):
        return True, 0, 0  # 무제한
    rows = await db.list_watch(user_id)
    current = len(rows)
    return current < FREE_WATCHLIST_MAX, current, FREE_WATCHLIST_MAX

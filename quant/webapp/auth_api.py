"""가입 · 로그인의 HTTP 표면 — 세션 쿠키 하나로 사람을 알아봅니다.

`accounts.py` 가 저장을 맡고, 이 파일은 그것을 브라우저에 연결합니다. 여기서
정해지는 것은 네 가지입니다.

**쿠키.** 이름은 `__Host-` 로 시작하고, HttpOnly(스크립트가 못 읽음), Path=/,
https 로 들어온 요청이면 Secure, 그리고 **SameSite=Lax** 입니다. SameSite 가
없으면 사용자가 열어둔 아무 페이지나 로그인된 세션으로 이쪽에 주문 요청을
보낼 수 있습니다. 대시보드는 매수·매도·전량청산 버튼을 가지고 있고, 그
버튼은 남의 사이트에서 눌려서는 안 됩니다. `__Host-` 는 옆 서브도메인이 같은
이름의 쿠키를 끼워넣지 못하게 합니다. 그래도 한 요청에 세션 쿠키가 두 장
오면 어느 쪽도 세션으로 보지 않습니다 — 정상 브라우저는 한 장만 보냅니다.

**구별되지 않는 실패.** 없는 이메일과 틀린 비밀번호는 호출자에게 완전히 같은
응답입니다. 둘을 구별해주면 "이 이메일이 이 서비스에 가입돼 있다"는 목록을
로그인 폼만으로 뽑아낼 수 있고, 그 목록이 자격증명 대입의 절반입니다.
무엇이 있었는지는 감사 로그만 압니다.

**시도 제한.** 증권사 키를 들고 있는 서비스의 로그인 폼은 크리덴셜 스터핑의
1순위 표적입니다. (이메일, 주소) 쌍별·주소별로 실패를 세고 잠그며, 가입도
같은 저울에 올립니다. 그리고 이 프로세스에는 모든 사용자의 봇이 함께 살기
때문에, 비밀번호 해시는 이벤트 루프 밖(스레드풀)에서 돌립니다 — 루프가 멈추면
멈추는 것은 로그인이 아니라 손절과 전량청산 버튼입니다.

**주소는 프록시가 말해줄 때만 믿습니다.** `X-Forwarded-For` 는 아무나 쓸 수
있는 헤더라, `QUANT_TRUSTED_PROXIES` 에 적힌 피어에서 온 요청에서만 읽습니다.
그 위에 어떤 버킷과도 무관한 전역 상한을 하나 둡니다 — 버킷을 잘못 나눠도
"한도가 아예 없음"이 되지는 않도록.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from quant.webapp.accounts import AccountError, Accounts, User

log = logging.getLogger("quant.webapp.auth")

#: 쿠키 이름. 다른 라우터가 같은 이름을 봐야 하므로 상수로 내놓습니다.
#
# The `__Host-` prefix is not decoration: a browser refuses to store a cookie
# under this name unless it is Secure, Path=/ and carries **no** Domain. That
# is exactly what stops a sibling subdomain (or anything with a
# response-header-injection foothold on one) from writing a second
# `quant_session` into the victim's jar. Every read site keys off this
# constant, so the protection reaches them all at once.
#
# Cost: over plain http we cannot send Secure (see `_arrived_over_https`), and
# a browser then drops the cookie. Local development therefore wants either
# https or `QUANT_COOKIE_SECURE=1` — browsers accept Secure on `localhost`.
# `_set_cookie` says so out loud rather than letting login fail in silence.
SESSION_COOKIE = "__Host-quant_session"

# Keep this in step with accounts._SESSION_DAYS. Drift is not dangerous in the
# risky direction: the `sessions` row is the authority, so a cookie that
# outlives it simply gets a 401 and is cleared.
COOKIE_MAX_AGE = 30 * 24 * 60 * 60

#: 로그인 실패는 언제나 이 한 문장입니다. 아래 두 경우를 구별하지 않습니다.
BAD_LOGIN = "이메일 또는 비밀번호가 올바르지 않습니다"

#: 429 로 돌려보낼 때의 문장. {seconds} 는 Retry-After 와 같은 값입니다.
TOO_MANY_LOGINS = "로그인 시도가 너무 잦습니다. {seconds}초 후 다시 시도하세요"
TOO_MANY_SIGNUPS = "가입 시도가 너무 잦습니다. {seconds}초 후 다시 시도하세요"


# ── 요청 본문 ────────────────────────────────────────────────────────────
# Every string is length-capped. A password field with no ceiling is a free
# CPU grant: PBKDF2 hashes whatever arrives, so a few megabytes of "password"
# per request is a denial of service that costs the sender nothing.
class RegisterRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)
    display_name: str = Field("", max_length=64)


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=1024)


class PasswordRequest(BaseModel):
    """현재 비밀번호를 알아야 바꿀 수 있습니다."""

    # The UI may send either {current, new} or {current_password,
    # new_password}; both spellings are accepted so the screen and the router
    # do not have to agree on one first.
    model_config = ConfigDict(populate_by_name=True)

    current: str = Field(max_length=1024, alias="current_password")
    new: str = Field(max_length=1024, alias="new_password")


def public_user(user: User) -> dict:
    """화면에 내보내도 되는 사용자 필드 — 자격증명은 여기에 없습니다."""
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "created_at": user.created_at.isoformat(),
    }


# ── 시도 제한 ────────────────────────────────────────────────────────────
class LoginRateLimiter:
    """실패한 로그인과 가입 시도를 세는 슬라이딩 윈도우.

    잠그는 버킷은 셋입니다. **(이메일, 주소) 쌍** 이 가장 촘촘하고, **주소**
    가 그 위, 그리고 어떤 분류와도 무관한 **전역** 상한이 맨 위입니다. 마지막
    것은 버킷을 잘못 나눴을 때 "한도가 아예 없음"으로 무너지지 않게 하는
    바닥이고, 실제로 걸리면 로그인·가입만 잠깁니다 — 이미 로그인한 사람의
    세션도, 돌고 있는 봇도 건드리지 않습니다.

    이메일만으로는 잠그지 않습니다. 그렇게 하면 남이 아무 데서나 다섯 번
    틀려주는 것으로 주인을 자기 계정에서 쫓아낼 수 있습니다. 대신 이메일별
    실패 수는 세어두고, 그 이메일에 여러 주소에서 실패가 몰릴 때 **틀린
    시도의 답을 늦추는 데만** 씁니다(`delay_for`). 맞는 비밀번호는 언제나
    지나갑니다.

    프로세스 안에만 있고, 일부러 단순합니다. **버티지 못하는 것**: 재시작(카운터가
    비워집니다)과 다중 워커(프로세스마다 자기 카운터를 세므로 워커 N개는 한도
    N배입니다). 하나의 상자 위에 놓는 과속방지턱이지 분산 쿼터가 아닙니다 —
    워커를 늘릴 때는 진짜 제한을 프록시나 공용 저장소에 두어야 합니다.
    """

    def __init__(self, *, per_email: int = 5, per_address: int = 20,
                 per_register: int = 5, per_global: int = 600,
                 window_s: float = 300.0, delay_s: float = 2.0,
                 clock: Callable[[], float] = time.monotonic):
        self.per_email = per_email
        self.per_address = per_address
        self.per_register = per_register
        self.per_global = per_global
        self.window_s = window_s
        self.delay_s = delay_s
        self._clock = clock
        self._fails: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    # ── 로그인 ───────────────────────────────────────────────────────────
    def retry_after(self, email: str, address: str) -> float:
        """통과하면 0.0, 잠겨 있으면 몇 초 뒤에 오면 되는지."""
        return self._retry_after(self._login_caps(email, address))

    def fail(self, email: str, address: str) -> None:
        self._charge(self._login_keys(email, address))

    def succeed(self, email: str, address: str) -> None:
        with self._lock:
            # Only this caller's own buckets are forgiven. Stuffing walks
            # thousands of emails from one address, and one lucky hit among
            # them is exactly the moment the address counter matters most.
            self._fails.pop(_pair_key(email, address), None)
            self._fails.pop(("email", _norm_email(email)), None)

    def delay_for(self, email: str) -> float:
        """이 이메일에 실패가 몰릴 때 붙일 지연(초). 잠그지는 않습니다.

        A hard per-email lockout hands a stranger the owner's account door:
        five wrong guesses from anywhere and she is out for the window. A
        delay costs a distributed guesser their throughput and costs the owner
        nothing, because it is only ever applied to an attempt that already
        failed.
        """
        if self.delay_s <= 0 or self.per_email <= 0:
            return 0.0
        now = self._clock()
        with self._lock:
            over = len(self._recent(("email", _norm_email(email)), now)) - self.per_email
        return min(self.delay_s, 0.5 * over) if over > 0 else 0.0

    # ── 가입 ─────────────────────────────────────────────────────────────
    def register_retry_after(self, address: str) -> float:
        """가입도 로그인과 같은 저울에 답니다 — 비용이 같기 때문입니다."""
        return self._retry_after(self._register_caps(address))

    def register_attempt(self, address: str) -> None:
        """가입은 실패만이 아니라 **시도마다** 답니다.

        A registration that succeeds costs the same 600,000 rounds as one that
        fails, and a walk through a candidate list is made entirely of
        successful-looking probes.
        """
        self._charge([key for key, _cap in self._register_caps(address)])

    # ── 공통 ─────────────────────────────────────────────────────────────
    def _retry_after(self, caps: list[tuple[tuple[str, str], int]]) -> float:
        now = self._clock()
        with self._lock:
            wait = 0.0
            for key, cap in caps:
                stamps = self._recent(key, now)
                if len(stamps) >= cap:
                    # The cap-th newest failure is the one that has to age out
                    # of the window before another attempt fits under the cap.
                    wait = max(wait, self.window_s - (now - stamps[-cap]))
            return max(wait, 0.0)

    def _charge(self, keys: list[tuple[str, str]]) -> None:
        now = self._clock()
        with self._lock:
            self._sweep(now)
            for key in keys:
                self._fails.setdefault(key, []).append(now)

    def _login_caps(self, email: str, address: str) -> list[tuple[tuple[str, str], int]]:
        out = []
        if _norm_email(email) and self.per_email > 0:
            out.append((_pair_key(email, address), self.per_email))
        if address and self.per_address > 0:
            out.append((("addr", address), self.per_address))
        out.extend(self._global_cap())
        return out

    def _login_keys(self, email: str, address: str) -> list[tuple[str, str]]:
        keys = [key for key, _cap in self._login_caps(email, address)]
        if _norm_email(email):
            # Counted, never blocking — `delay_for` is the only reader.
            keys.append(("email", _norm_email(email)))
        return keys

    def _register_caps(self, address: str) -> list[tuple[tuple[str, str], int]]:
        out = []
        if address and self.per_register > 0:
            out.append((("reg", address), self.per_register))
        if address and self.per_address > 0:
            # Shared with login on purpose: an address that just burned its
            # login budget does not get a fresh one by switching endpoints.
            out.append((("addr", address), self.per_address))
        out.extend(self._global_cap())
        return out

    def _global_cap(self) -> list[tuple[tuple[str, str], int]]:
        return [(("all", ""), self.per_global)] if self.per_global > 0 else []

    def _recent(self, key: tuple[str, str], now: float) -> list[float]:
        stamps = [t for t in self._fails.get(key, ()) if now - t < self.window_s]
        if stamps:
            self._fails[key] = stamps
        else:
            self._fails.pop(key, None)
        return stamps

    def _sweep(self, now: float) -> None:
        # A stuffing run submits thousands of distinct emails; without this the
        # bucket dict is an unbounded memory grant to whoever fills the form.
        if len(self._fails) > 4096:
            for key in list(self._fails):
                self._recent(key, now)


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _pair_key(email: str, address: str) -> tuple[str, str]:
    # NUL cannot occur in either half, so the two never run together into an
    # ambiguous key.
    return ("pair", f"{_norm_email(email)}\x00{address}")


def _trusted_proxies() -> frozenset[str]:
    """`QUANT_TRUSTED_PROXIES` — 쉼표나 공백으로 나열한 피어 주소."""
    raw = os.environ.get("QUANT_TRUSTED_PROXIES", "").replace(",", " ")
    return frozenset(part for part in raw.split() if part)


def _client_address(request: Request) -> str:
    """레이트리밋 버킷으로 쓸 호출자 주소."""
    # X-Forwarded-For is a header, and a header is whatever the caller typed.
    # Reading it from an untrusted peer does not identify anyone — it hands
    # them a new bucket per request, which is the same as having no per-address
    # limit at all. So we read it only from a peer the operator has named, and
    # otherwise use the socket. `*` trusts every peer and is only correct when
    # something in front already overwrites the header.
    peer = request.client.host if request.client else ""
    trusted = _trusted_proxies()
    if peer and ("*" in trusted or peer in trusted):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return peer


def _cookie_values(header: str, name: str) -> list[str]:
    """`Cookie:` 헤더에서 이 이름으로 온 값 전부 — 몇 장인지가 중요합니다."""
    out = []
    for part in header.split(";"):
        key, sep, value = part.partition("=")
        if sep and key.strip() == name:
            out.append(value.strip().strip('"'))
    return out


def _too_many(wait: float, message: str) -> HTTPException:
    """잠긴 동안의 답 — 얼마나 기다리면 되는지는 본문과 헤더 양쪽에."""
    seconds = int(wait) + 1
    return HTTPException(429, message.format(seconds=seconds),
                         headers={"Retry-After": str(seconds)})


def _arrived_over_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    # Never let a header take Secure *away*: a forged one can only add it, and
    # a Secure cookie on a plain-http deployment merely stops working, which is
    # the failure everybody notices immediately rather than the silent one.
    if request.url.scheme == "https" or proto == "https":
        return True
    return os.environ.get("QUANT_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")


_warned_about_secure = False


def _warn_host_prefix_needs_secure(name: str) -> None:
    """한 번만, 그러나 조용히 지나가지는 않게."""
    # Without Secure the browser silently discards a `__Host-` cookie, and the
    # symptom is "login appears to work and then I am logged out" — the kind of
    # thing an operator debugs for an hour. Say it once, plainly.
    global _warned_about_secure
    if not _warned_about_secure:
        _warned_about_secure = True
        log.warning(
            "%s 쿠키를 Secure 없이 보냈습니다 — 브라우저는 이 쿠키를 저장하지 "
            "않습니다. https 뒤에 두거나, 로컬 개발이라면 QUANT_COOKIE_SECURE=1 "
            "을 켜세요 (브라우저는 localhost 에서 Secure 쿠키를 받아줍니다).", name)


# ── 라우터 + 의존성 ──────────────────────────────────────────────────────
class Auth:
    """마운트할 라우터와, 다른 라우터가 쓸 의존성을 함께 들고 있는 묶음.

        auth = build_auth(accounts)
        app.include_router(auth.router)

        @app.get("/api/positions")
        async def positions(user: User = Depends(auth.current_user)): ...
        @app.get("/api/admin/users")
        async def users(admin: User = Depends(auth.require_admin)): ...
    """

    def __init__(self, accounts: Accounts, *, cookie: str = SESSION_COOKIE,
                 limiter: LoginRateLimiter | None = None):
        self.accounts = accounts
        self.cookie = cookie
        self.limiter = limiter if limiter is not None else LoginRateLimiter()
        self.router = APIRouter(prefix="/api/auth", tags=["auth"])
        self._install()

    # ── 의존성 ───────────────────────────────────────────────────────────
    # FastAPI actually evaluates the annotations of anything it depends on, and
    # `from __future__ import annotations` leaves them as strings until it does.
    # `X | None` only evaluates on 3.10+, so dependency signatures stay on
    # `Optional[...]` — the rest of the file may use either.
    async def optional_user(self, request: Request) -> User | None:
        """로그인했으면 그 사람, 아니면 None. 공개/비공개가 섞인 화면용."""
        return self.accounts.user_for_session(self._session_token(request))

    def _session_token(self, request: Request) -> str:
        """쿠키가 두 장이면 세션으로 보지 않습니다.

        A cookie parser keeps one of the duplicates — Starlette's keeps the
        last — so an attacker who can append a second `Cookie:` entry decides
        who the victim is signed in as, and the onboarding screen's next
        question is the victim's 한국투자증권 앱키. A real browser sends one.
        The `__Host-` prefix is what keeps a sibling subdomain from writing the
        second one; this is the belt to that pair of braces.
        """
        values = _cookie_values(request.headers.get("cookie", ""), self.cookie)
        if len(values) > 1:
            log.warning("한 요청에 %s 쿠키가 %d 장 왔습니다 — 세션으로 보지 않습니다",
                        self.cookie, len(values))
            return ""
        return values[0] if values else ""

    async def current_user(self, request: Request) -> User:
        user = await self.optional_user(request)
        if user is None:
            raise HTTPException(401, "로그인이 필요합니다")
        return user

    async def require_admin(self, request: Request) -> User:
        user = await self.current_user(request)
        if not user.is_admin:
            # 403, not 404: they are a real signed-in user, they just are not
            # this one. Hiding the route buys nothing once the cookie is valid.
            raise HTTPException(403, "관리자만 접근할 수 있습니다")
        return user

    # ── 쿠키 ─────────────────────────────────────────────────────────────
    def _set_cookie(self, request: Request, response: Response, token: str) -> None:
        secure = _arrived_over_https(request)
        if self.cookie.startswith("__Host-") and not secure:
            _warn_host_prefix_needs_secure(self.cookie)
        response.set_cookie(
            self.cookie, token,
            max_age=COOKIE_MAX_AGE,
            path="/",             # __Host- 가 요구하는 값이기도 합니다
            httponly=True,        # 스크립트가 세션 토큰을 읽지 못하게
            samesite="lax",       # 남의 사이트에서 온 POST 에는 쿠키를 붙이지 않게
            secure=secure,
        )
        # No Domain= is set anywhere in this file, and that is load-bearing for
        # the `__Host-` prefix: the moment a Domain appears the browser stops
        # storing the cookie at all.

    def _clear_cookie(self, request: Request, response: Response) -> None:
        # Same attributes as when it was set — a browser matches on name, path
        # and domain, so a mismatch leaves the old cookie sitting there.
        response.delete_cookie(
            self.cookie, path="/", httponly=True, samesite="lax",
            secure=_arrived_over_https(request),
        )

    # ── 엔드포인트 ───────────────────────────────────────────────────────
    def _install(self) -> None:
        accounts = self.accounts

        @self.router.post("/register", status_code=201)
        async def register(req: RegisterRequest, request: Request, response: Response):
            address = _client_address(request)
            wait = self.limiter.register_retry_after(address)
            if wait > 0:
                # Refused before the KDF runs. A rejection has to be cheap for
                # *us*; whether it is cheap for the sender does not matter.
                raise _too_many(wait, TOO_MANY_SIGNUPS)
            self.limiter.register_attempt(address)
            try:
                user = await run_in_threadpool(
                    accounts.register, req.email, req.password, req.display_name)
            except AccountError as exc:
                # 이미 가입된 이메일이면 그렇게 말해줍니다 — 그리고 그것이
                # 그대로 명부 조회 창구입니다. What this endpoint can and
                # cannot hide, plainly:
                #
                #   Hidden : bulk enumeration. Every attempt, taken email or
                #            not, spends the caller's per-address budget above
                #            and lands in the audit log as
                #            `register_taken_email`, so walking a candidate
                #            list costs an address per handful of probes and
                #            is visible afterwards.
                #   NOT hidden: membership of one email you already care
                #            about. A signup form that must hand a real person
                #            an immediate session cannot answer identically for
                #            "free" and "taken" — the session itself is the
                #            answer. Only out-of-band verification closes that,
                #            and this project has no mail sender to do it with;
                #            when one exists, the fix is to return the same
                #            "확인 메일을 보냈습니다" either way and let the mail
                #            carry the difference.
                if accounts.by_email(req.email) is not None:
                    await run_in_threadpool(
                        accounts.record, None, "register_taken_email", req.email.strip())
                raise HTTPException(400, str(exc)) from None
            token = await run_in_threadpool(accounts.create_session, user.id)
            self._set_cookie(request, response, token)
            return public_user(user)

        @self.router.post("/login")
        async def login(req: LoginRequest, request: Request, response: Response):
            email = req.email.strip()
            address = _client_address(request)
            wait = self.limiter.retry_after(email, address)
            if wait > 0:
                # 429 이지 401 이 아닙니다 — 잠긴 것과 틀린 것은 다른 상황이고,
                # 사람은 얼마나 기다리면 되는지 알아야 합니다.
                raise _too_many(wait, TOO_MANY_LOGINS)

            # 600,000 rounds of PBKDF2, off the loop. On the loop it is not a
            # slow login, it is every user's trader task standing still: no bar
            # processed, no stop-loss fired, no 전량청산 button answering.
            #
            # Every *writing* Accounts call goes the same way, not only the two
            # that hash. The store serialises its writers behind one lock and
            # `register` holds that lock across the hash, so a `create_session`
            # left on the loop waits out somebody else's KDF and the freeze
            # comes straight back through the side door.
            user = await run_in_threadpool(accounts.authenticate, email, req.password)
            if user is None:
                self.limiter.fail(email, address)
                # The caller cannot tell these two apart; the audit log can.
                # accounts.authenticate already records the known-email case
                # against that user, so only the unknown one is left to note.
                if accounts.by_email(email) is None:
                    await run_in_threadpool(
                        accounts.record, None, "login_failed_unknown_email", email)
                # 한 이메일에 여러 주소에서 실패가 몰릴 때만 늦춥니다. 잠그지
                # 않으므로 주인은 맞는 비밀번호로 언제든 들어옵니다.
                delay = self.limiter.delay_for(email)
                if delay > 0:
                    await asyncio.sleep(delay)
                raise HTTPException(401, BAD_LOGIN)

            self.limiter.succeed(email, address)
            token = await run_in_threadpool(accounts.create_session, user.id)
            self._set_cookie(request, response, token)
            return public_user(user)

        @self.router.post("/logout")
        async def logout(request: Request, response: Response):
            # Deliberately unauthenticated and idempotent: "log me out" must
            # work even when the session is already dead or the cookie is junk.
            # Duplicates are revoked one and all — the caller asked to be
            # logged out, and which of the two was theirs is the open question.
            for token in _cookie_values(request.headers.get("cookie", ""), self.cookie):
                if token:
                    await run_in_threadpool(accounts.revoke, token)
            self._clear_cookie(request, response)
            return {"ok": True}

        @self.router.get("/me")
        async def me(user: User = Depends(self.current_user)):
            return public_user(user)

        @self.router.post("/password")
        async def change_password(req: PasswordRequest, request: Request,
                                  response: Response,
                                  user: User = Depends(self.current_user)):
            try:
                # Two hashes (verify the old one, stretch the new one) — the
                # most expensive authenticated call in the service.
                await run_in_threadpool(
                    accounts.change_password, user.id, req.current, req.new)
            except AccountError as exc:
                raise HTTPException(400, str(exc)) from None
            # change_password revokes every session, this one included. The
            # person at the keyboard just proved they know the old password, so
            # hand them a fresh session; every *other* device stays logged out,
            # which is the whole point of changing it.
            token = await run_in_threadpool(accounts.create_session, user.id)
            self._set_cookie(request, response, token)
            return {"ok": True, "message": "비밀번호를 바꿨습니다. 다른 기기는 모두 로그아웃됩니다."}


def build_auth(accounts: Accounts, *, cookie: str = SESSION_COOKIE,
               limiter: LoginRateLimiter | None = None) -> Auth:
    """Accounts 하나로 라우터와 의존성을 만듭니다 — 마운트하는 쪽의 진입점."""
    return Auth(accounts, cookie=cookie, limiter=limiter)


def build_auth_router(accounts: Accounts, **kwargs) -> APIRouter:
    """라우터만 필요할 때. 의존성도 쓸 거라면 build_auth 를 쓰세요."""
    return build_auth(accounts, **kwargs).router


__all__ = [
    "Auth", "LoginRateLimiter", "LoginRequest", "PasswordRequest",
    "RegisterRequest", "BAD_LOGIN", "COOKIE_MAX_AGE", "SESSION_COOKIE",
    "TOO_MANY_LOGINS", "TOO_MANY_SIGNUPS",
    "build_auth", "build_auth_router", "public_user",
]

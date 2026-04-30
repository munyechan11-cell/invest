# Toss — 단기투자 어시스턴트 (Web)

티커 + 투자금을 입력하면 실시간 시세·뉴스를 종합해 퀀트 리포트를 띄우고,
목표가/손절가 도달 시 브라우저 알림으로 "지금 분할매수!" / "지금 매도!" 를 쏘는 웹 대시보드.

## 구성
- **Backend:** FastAPI + SQLite + 백그라운드 알림 워커 (`server/`)
- **Frontend:** 단일 HTML(Tailwind+Alpine.js), `/` 에서 서빙 (`server/static/index.html`)
- **분석 엔진:** 기존 `app/` 모듈 (Claude Opus 4.7로 시세+뉴스+수급 종합)
- **실시간:** WebSocket(`/ws`)로 가격 틱·알림 푸시. 워커가 6초마다 Finnhub `/quote` 폴링
- **Telegram 봇(옵션):** `python -m app.bot` 으로 별도 실행 가능

## 셋업
```
pip install -r requirements.txt
copy .env.example .env   # 키 4개 채우기
python -m server.main
```
브라우저: http://127.0.0.1:8000

## 사용 흐름
1. 좌측 "워치리스트"에서 티커 + 투자금(USD) + 리스크%(기본 1) 입력 → 추가
2. 자동으로 분석 실행 → 중앙 패널에 퀀트 리포트 + 권장 주식수/분할 매수계획 표시
3. 우측 "실시간 알림"의 [알림 권한 켜기] 클릭
4. 가격이 분석에서 나온 진입가/목표가/손절가 도달 시 브라우저 푸시 + 사운드

## 알림 트리거
- **BUY:** 포지션이 매수계열 + 가격이 재진입가 ±0.3% 도달
- **TP:** 가격 ≥ 목표가
- **SL:** 매수계열 + 가격 ≤ 손절가
- **SELL:** 매도계열 + 가격 ≥ 목표가
같은 알림은 5분 쿨다운.

## API 키
`.env.example` 참고. 필수: Gemini(또는 Anthropic), Alpaca, Finnhub, KIS, Naver, DART.

## 배포 (Render — 무료티어 가능)
1. 이 repo를 GitHub에 푸시
2. https://render.com 가입 → New → Blueprint → 이 repo 선택
3. `render.yaml` 자동 인식 → 환경변수 입력란에 `.env`의 키들 그대로 붙여넣기
4. `JWT_SECRET_KEY`는 Render가 자동 생성, `ADMIN_PASSWORD`는 강한 비번으로 직접 입력
5. Deploy → `https://toss-quant.onrender.com` 발급
> 무료티어는 15분 무사용 시 슬립. 깨어날 때 30초 지연. 24/7 원하면 Starter $7/월.

## 배포 (Railway 대안)
1. https://railway.app → New Project → Deploy from GitHub
2. Variables 탭에 `.env` 키 입력
3. Settings → Networking → Generate Domain

## 한계
- 다크풀 정확 데이터는 무료 소스 부재 — 거래량 vs 20일평균 + 뉴스/인사이더로 추론
- 실거래 자동매매는 의도적으로 OFF. `/buy`, `/sell` 텔레그램 명령으로만 페이퍼 트레이딩 가능
- 본 도구는 참고용. 최종 매매 책임은 사용자.

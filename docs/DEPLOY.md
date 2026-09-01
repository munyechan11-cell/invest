# 폐기된 배포 문서 — 현재 절차로 이동하세요

이 파일에 과거에 있던 Render와 `QUANT_API_TOKEN`/`?token=` 절차는 **구현과
맞지 않는 구문서**라 폐기했습니다. Git 이력에서 옛 명령을 찾아 실행하지
마세요.

현재 운영 기준은 다음 두 문서입니다.

1. [배포 정책과 실거래 체크리스트](../DEPLOY.md)
2. [고정 IP VPS 설치·운영 절차](../deploy/README.md)

현재 `siftai.kr`의 Toss 실거래는 고정 egress IP가 있는 VPS를 사용합니다.
Render Starter/Standard의 공유 IP는 Toss 허용 IP 계약과 맞지 않습니다.
제어 API 인증도 공유 query token이 아니라 로그인 세션의 `__Host-` cookie를
사용하므로 `QUANT_API_TOKEN`을 만들거나 URL에 `?token=`을 붙이지 마세요.

Render 설정(`render.yaml`)은 IP 제한이 없는 provider를 별도로 운영할 때만
루트 `DEPLOY.md`의 현재 제한을 확인한 뒤 사용합니다.

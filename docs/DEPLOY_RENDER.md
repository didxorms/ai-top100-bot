# Render에 카카오톡 봇 배포하기

별도 도메인을 구매하지 않고 Render가 발급하는 고정 HTTPS 주소를 사용합니다.
구조는 카카오톡 → Render의 HTTPS 주소 → Python 봇 → OpenAI → 카카오톡입니다.
노트북이나 학교 서버의 가동 상태에 의존하지 않도록 Render에서 봇과 SQLite DB를 함께 운영합니다.

## 생성된 서버

- 공개 주소: https://ai-top100-bot.onrender.com
- 상태 점검: https://ai-top100-bot.onrender.com/healthz
- 카카오 웹훅: https://ai-top100-bot.onrender.com/kakao
- 관리 화면: https://dashboard.render.com/web/srv-dbgk7um7bikc73ae2e80
- 서비스 ID: `srv-dbgk7um7bikc73ae2e80`
- 영구 디스크: `bot-state`, `/data`, 1GB
- 최초 배포 코드: `6af3f96813aad1341a060c7295a991f7ed7c0ea2`
- OpenAI 모드 배포 코드: `7898d067793e7fd9927871c6e5cc5e34c6be766c`
- 현재 모드: `LLM_MODE=openai`, `ACTION_MODE=off`
- 현재 모델: `OPENAI_MODEL=gpt-6-luna` (2026-10-29 기존 유효 키로 응답 확인 후 반영)
- 웹 검색: `WEB_SEARCH_MODE=auto` (2026-10-29 추가, 최신 공개 정보는 OpenAI 공식 검색 도구 사용)

카카오 에코 답장 검증 후 `openai`로 전환했고, 공개 `/healthz` 응답을 확인했습니다.
서버 로그에서도 실제 OpenAI 답변 생성 성공과 카카오 콜백 HTTP 200·전송 성공을 확인했습니다.
노트북과 학교 서버를 실행하지 않아도 Render에서 봇을 운영할 수 있습니다.

## 준비된 설정

`render.yaml`에 다음 구성을 준비했습니다. 파일을 준비한 것만으로 유료 서버가 생성되지는 않습니다.

| 항목 | 설정 |
|---|---|
| GitHub 저장소 | `https://github.com/didxorms/ai-top100-bot` |
| 브랜치 | `main` |
| 런타임 | Docker, 루트의 `Dockerfile` 사용 |
| 실행 명령 | Dockerfile의 `python serve.py` |
| 리전 | Singapore |
| 인스턴스 | 1개, `0.5c-512mb` (0.5 CPU, 512MB) |
| 영구 디스크 | `/data`, 1GB |
| 상태 점검 | `/healthz` |
| 종료 대기 | 영구 디스크 연결 서비스는 Render 기본 종료 정책 적용. 사용자 지정 종료 대기 확장은 지원되지 않음 |
| 자동 코드 배포 | 비활성화, 변경 사항은 검증 후 수동 배포 |
| 초기 응답 모드 | `LLM_MODE=echo`, 액션 실습은 `ACTION_MODE=off` |

2026-10-28 확인한 [공식 요금](https://render.com/pricing) 기준 서버는 월 환산 7달러, 디스크는 GB당 월 환산 0.25달러입니다.
Hobby 워크스페이스에 유료 서버 하나와 디스크 1GB를 운영하면 월 환산 7.25달러가 기본 구성입니다.
[실제 가동 기간과 스토리지 사용 기간에 비례해 과금](https://render.com/docs/faq)되며, 세금·추가 트래픽·빌드 사용량·OpenAI 비용은 별도일 수 있습니다.
Free 서버는 유휴 종료와 영구 디스크 제약이 있어 이번 심사 운영에는 사용하지 않습니다.

## 계정과 배포

1. [Render](https://dashboard.render.com/)에서 본인 계정을 만들거나 로그인합니다.
2. GitHub 계정을 연결해 해당 저장소를 선택합니다. 저장소에 최신 로컬 코드가 업로드되었는지 확인합니다.
3. Blueprint를 사용하는 경우 `render.yaml`을 선택합니다. `BOT_TOKEN`, `OPENAI_API_KEY`는 입력 화면에만 등록합니다.
4. 유료 인스턴스와 디스크를 확인하고 배포합니다. 필요하면 본인 브라우저에서 결제 수단을 등록합니다.
5. 발급된 `https://실제서비스주소.onrender.com/healthz`에서 `{"status":"ok"}`를 확인합니다.

API 키와 토큰은 GitHub, `render.yaml`, 채팅에 넣지 않습니다.
CLI를 사용할 때도 비밀 값은 파일 또는 표준 입력을 통해 전달하며 명령줄에 노출하지 않습니다.
관리 화면에서 직접 Web Service를 만드는 경우에도 표의 설정을 동일하게 적용합니다.
`render.yaml`은 Blueprint용 설정 파일이므로 수동 생성 서비스에 자동 적용된다고 가정하지 않습니다.

## 카카오 연결과 검증

1. 카카오 웹훅을 `https://실제서비스주소.onrender.com/kakao`로 변경합니다.
2. 카카오톡에서 질문을 보내 `[연동 테스트]` 답장을 확인합니다.
3. Render 환경변수의 `LLM_MODE`를 `openai`로 변경하고 수동 배포합니다.
4. 실제 답변, 여러 차례의 대화, 다른 채팅방의 분리를 확인합니다.
5. 서버를 재시작해도 같은 DB와 대화 기억이 유지되는지 확인합니다.
6. 노트북의 Python 서버와 터널을 꺼도 답장이 오는지 확인합니다.

새 서버는 별도 DB로 시작합니다. 기존 노트북의 기억을 이전하려면 `python -m scripts.backup_state`로 백업하고,
Render의 기존 DB가 있다면 먼저 별도로 백업한 뒤 서버가 중지된 상태에서 영구 디스크에 복원합니다.
실행 중인 SQLite DB를 파일로 덮어쓰지 않습니다.

## 수정과 최종 제출

- 기능 수정 후 테스트 → GitHub 업로드 → Render 수동 배포 → 카카오톡 검증 순으로 진행합니다.
- 자동 코드 배포는 꺼둡니다. Blueprint를 사용한다면 최종 제출 전에 Blueprint 자동 동기화도 비활성화합니다.
- 영구 디스크 연결 서비스는 재배포 시 기존 인스턴스를 먼저 종료하므로 응답이 일시 중단됩니다.
  이번 OpenAI 모드 전환은 빌드 시작부터 정상 가동까지 약 4분 34초가 걸렸습니다.
  모든 수정과 재배포 검증은 최종 제출 전에 마칩니다.
- 제출 전에 코드 커밋과 환경변수를 기록하고, 대회 당일 제공되는 실제 액션 API로 교체해 검증합니다.
- 제출 이후에는 심사 종료까지 시나리오, 프롬프트, 코드, 환경변수와 배포 상태를 변경하거나 삭제하지 않습니다.
- 2026 예선 운영 기한은 11월 1일 24:00(한국 시각)입니다. 결제와 가동 상태를 그 이후까지 확보합니다.

공식 참고: [웹 서비스](https://render.com/docs/web-services), [Docker](https://render.com/docs/docker),
[영구 디스크](https://render.com/docs/disks), [Blueprint 설정](https://render.com/docs/blueprint-spec).

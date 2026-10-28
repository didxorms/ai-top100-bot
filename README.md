# AI TOP 100 카카오톡 봇

카카오톡 웹훅을 받아 OpenAI Responses API로 답변하고 카카오 콜백 API로 전송합니다.

## 폴더 구성

```text
bot/                  봇 실행 코드, 대화 기억, 액션 API 연동
tests/                자동 검사
docs/                 학교·Render·Railway 배포 및 액션 실습 안내
scripts/              DB 백업 등 운영 도구
tools/                로컬에서 사용하는 Cloudflare 실행 파일
serve.py              학교 서버와 클라우드의 실행 진입점
requirements.txt      Python 패키지 목록
Dockerfile            컨테이너 실행 환경
railway.json          Railway 배포 설정
render.yaml           Render 배포 설정
.env                  로컬 인증 정보와 설정 (Git 제외)
.local/               대화 DB·백업·진단 자료 (Git 제외)
```

모든 명령은 프로젝트 루트 폴더에서 실행합니다. `.env`와 `.local/`의 위치는 폴더 정리 전과 같습니다.

## 실행

`.env`에서 `BOT_TOKEN`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `LLM_MODE`, `KAKAO_TEXT_PATH`를 설정합니다.
실제 AI 대화는 `LLM_MODE=openai`, 연결 테스트는 `LLM_MODE=echo`입니다.
현재 확인한 사용자 질문 경로는 `KAKAO_TEXT_PATH=d.content`입니다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn bot.app:app --host 127.0.0.1 --port 8000
```

다른 창에서는 Cloudflare 터널을 실행해 둡니다.

```powershell
.\tools\cloudflared.exe tunnel --url http://127.0.0.1:8000
```

코드나 `.env` 변경 후에는 Python 서버를 재시작합니다.
학교 서버의 실행 명령은 기존처럼 `python serve.py`를 사용합니다.

외부 서버로 옮기는 절차는 [학교 서버 배포 안내](docs/DEPLOY_SCHOOL.md)에 정리했습니다.
상용 클라우드는 [Render 배포 안내](docs/DEPLOY_RENDER.md)를 기준으로 준비합니다.
[Railway 배포 안내](docs/DEPLOY_RAILWAY.md)도 대안으로 보관합니다.
`DATA_DIR`로 상태 저장 폴더를 바꿀 수 있으며, 외부 서버에서는 반드시 영구 볼륨 경로를 지정합니다.
배포 상태 점검 주소는 `/healthz`입니다.

## 액션 API 연동 실습

`ACTION_MODE=mock`을 설정하면 가상 행사 조회·신청을 연습할 수 있습니다. 기본값은 `off`입니다.
행사 조회 → 신청 준비 → 사용자의 확인 명령 → 신청 확정 흐름을 사용합니다.
실제 예약이나 결제는 발생하지 않으며, OpenAI 사용료는 기존과 같이 발생합니다.
켜는 방법과 테스트 순서는 [액션 API 실습 안내](docs/PRACTICE.md)를 참고합니다.

## 대화 기억

- [OpenAI 공식 대화 관리 기능](https://developers.openai.com/api/docs/guides/conversation-state)의 `previous_response_id`를 사용합니다.
- 첫 답변의 응답 ID를 저장하고, 다음 질문에서 그 ID를 전달해 이전 질문과 답변을 이어갑니다. 동작 지침은 요청마다 다시 전달합니다.
- 1:1 채널은 사용자별로, 그룹방은 방 전체가 하나의 문맥을 공유합니다. 다른 그룹방과 채널 대화는 분리됩니다.
- `.local/conversation-state.sqlite3`에는 해시로 만든 대화 식별자와 마지막 OpenAI 응답 ID만 저장합니다. 카카오 토큰과 대화 원문은 저장하지 않습니다.
- 액션 실습 모드를 켜면 같은 DB의 별도 테이블에 요청자 해시와 연습용 신청 정보도 저장합니다.
- 서버를 재시작해도 같은 DB 파일이 있으면 대화를 이어갑니다. 같은 방의 메시지는 순서대로 처리합니다. 현재 잠금은 단일 Uvicorn 프로세스용입니다.
- `echo` 모드는 OpenAI를 호출하거나 새로운 기억을 만들지 않습니다.

카카오톡으로 `/새대화`, `/초기화`, `/new` 중 하나를 보내면 다음 질문부터 새 문맥으로 시작합니다.
그룹방에서는 봇을 멘션해 명령을 보냅니다. 그룹방의 새 대화 명령은 그 방 전체의 문맥을 새로 시작합니다.
이 명령은 로컬의 응답 ID 연결을 해제하며, OpenAI에 저장된 기록을 삭제하는 명령은 아닙니다.

OpenAI 쪽에는 `store=true`로 질문과 답변이 저장됩니다. 공식 문서상 응답 객체의 기본 보관 기간은 30일입니다.
이전 문맥도 입력 토큰에 포함되어 과금되며 입력 한도가 적용됩니다. 장기 기억 추출이나 긴 대화의 자동 요약은 현재 구현하지 않았습니다.
이전 응답이 만료되거나 입력 한도에 도달하면 새 대화를 시작할 수 있습니다.
ChatGPT 내부 구현 전체를 복제한 것은 아니며, 공식 API가 제공하는 한 대화의 문맥 연결 기능을 적용했습니다.

## 확인 방법

1. 서버를 재시작하고 카카오톡으로 `내 이름은 민수이고 예산은 10만 원이야`를 보냅니다.
2. `내 이름과 예산이 뭐였지?`를 보내 앞의 정보를 반영하는지 확인합니다.
3. 서버를 재시작하고 다시 질문해 문맥이 이어지는지 확인합니다.
4. `/새대화`를 보낸 뒤 앞의 정보를 더 이상 참조하지 않는지 확인합니다.
5. 다른 채팅방의 대화가 섞이지 않는지 확인합니다.

실제 OpenAI 호출에는 사용 가능한 API 크레딧이 필요합니다. API 키와 토큰은 공유하지 않습니다.

## 모의 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

가짜 인증 정보와 가짜 HTTP 응답만 사용합니다. 실제 OpenAI나 카카오 API를 호출하지 않습니다.

## 대화 DB 백업

```powershell
.\.venv\Scripts\python.exe -m scripts.backup_state
```

기본 백업 위치는 `.local/backups/`입니다. `--output`으로 다른 파일 경로를 지정할 수 있습니다.

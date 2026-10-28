# 노트북에서 Railway로 옮기기

2026-10-28 현재 [Railway 공식 상태 페이지](https://status.railway.com/)에서 신규 가입 중단과 재개 일정 미정을 안내하고 있습니다.
이번 배포는 [Render 안내](DEPLOY_RENDER.md)로 진행하며, 아래 구성은 Railway 이용이 가능해졌을 때 사용할 대안입니다.

목표 구조는 카카오톡 → Railway의 고정 HTTPS 주소 → Python 봇 → OpenAI → 카카오톡입니다.
이전이 완료되면 노트북의 Python 서버와 Cloudflare 임시 터널을 꺼도 됩니다.
현재 로컬 소스에는 폴더 정리와 액션 실습 기능까지 포함되어 있습니다. 최초 배포는 CLI로 현재 폴더를 업로드합니다.
GitHub의 이전 버전과 혼동하지 않도록 배포에 사용한 소스 버전을 확인합니다.

## 비용과 운영 방식

[Railway 공식 요금](https://docs.railway.com/pricing/plans) 기준 Hobby는 월 최소 5달러이며 5달러의 리소스 사용량이 포함됩니다.
포함 사용량을 넘으면 초과분이 청구됩니다. OpenAI 사용료는 별도입니다.
서버 하나, 인스턴스 하나와 영구 볼륨으로 시작합니다. 유휴 상태에서 잠드는 Serverless는 끕니다.

## 준비된 파일

- `Dockerfile`: Python 실행 환경과 필요한 코드만 묶습니다. `.env`와 노트북의 DB는 넣지 않습니다.
- `requirements.txt`: 현재 사용 중인 라이브러리 버전을 고정합니다.
- `serve.py`: 클라우드가 지정한 포트로 실행하고, 종료 요청 시 처리 중인 작업이 마무리될 시간을 줍니다.
- `railway.json`: 인스턴스 1개, 절전 비활성화, `/data` 마운트 필수 조건, CPU 1코어·메모리 512MiB 상한, `/healthz` 점검, 재시작 정책과 종료 대기 시간을 지정합니다.
- `.railwayignore`: 업로드에서 `.env`, `.venv`, `.local`, 실행 파일 등을 제외합니다.

## 1. 계정과 서비스 만들기

1. [Railway](https://railway.com/)에 로그인하고 요금제를 확인합니다.
2. 프로젝트와 빈 서비스를 만들고 서비스 이름을 `kakao-bot`으로 정합니다.
3. 서비스에 영구 Volume을 연결하고 마운트 경로를 `/data`로 지정합니다.
4. Replica는 1개로 유지하고 Serverless는 끕니다.

`railway.json`에서 `/data`를 필수 마운트 경로로 지정했으므로, Volume을 먼저 연결하고 배포합니다.
메모리 상한은 예약 사용량이나 고정 요금이 아닙니다. 실제 사용량과 계정 요금제에 따라 과금됩니다.

카카오 웹훅은 아직 기존 주소로 유지합니다.

## 2. 서버 환경변수 입력

서비스의 Variables에 다음 항목을 입력합니다. `.env` 파일을 소스에 올리지 않고 값만 서버의 환경변수에 등록합니다.

| 이름 | 값 |
|---|---|
| `BOT_TOKEN` | 현재 봇 인증 토큰 |
| `OPENAI_API_KEY` | 현재 OpenAI API 키 |
| `OPENAI_MODEL` | 현재 사용하는 모델, 기본 `gpt-4.1-mini` |
| `LLM_MODE` | 첫 확인은 `echo`, 이후 `openai` |
| `KAKAO_TEXT_PATH` | `d.content` |
| `ACTION_MODE` | 첫 배포는 `off`, 가상 행사 실습을 할 때만 `mock` |
| `DATA_DIR` | `/data` |

`PORT`는 Railway가 지정하도록 둡니다. Volume 없이 `DATA_DIR=/data`만 설정하면 데이터 유지가 보장되지 않습니다.

## 3. 소스 업로드

PC에 Node.js가 있으므로 별도 전역 설치 없이 공식 CLI를 실행할 수 있습니다.

```powershell
npx --yes @railway/cli login
npx --yes @railway/cli link
npx --yes @railway/cli up --service kakao-bot
```

로그인 후 `link`에서 위에서 만든 프로젝트와 환경을 선택합니다.
업로드는 [공식 CLI 안내](https://docs.railway.com/cli/up)처럼 `.gitignore`와 `.railwayignore`를 따릅니다.
토큰을 명령줄이나 채팅에 붙여넣지 않습니다. GitHub 저장소는 필수가 아닙니다.

로그인은 본인 브라우저에서 완료합니다. 브라우저 자동 열기가 어려우면 `login --browserless`로 표시되는 링크를 사용합니다.
CLI에 비밀 환경변수를 설정할 때는 `variable set KEY --stdin --skip-deploys`로 표준 입력을 사용합니다.
`.env` 전체를 서버에 올리는 대신 필요한 키만 선별하고, `DATA_DIR`과 실행 모드는 서버용 값으로 지정합니다.

Node.js가 Windows의 신뢰된 인증서를 사용해야 하는 환경이라면 현재 PowerShell 창에서 아래처럼 설정할 수 있습니다.
이 설정은 TLS 검증을 유지하며 `strict-ssl=false`나 인증서 검증 해제는 사용하지 않습니다.

```powershell
$env:NODE_USE_SYSTEM_CA = '1'
```

배포 후 서비스의 Networking에서 공개 도메인을 생성하고, 표시된 HTTPS 주소의 `/healthz`가 `{"status":"ok"}`를 반환하는지 확인합니다.

## 4. 기존 대화 기억 이전

기존 기억을 이어가려면 최종 전환 직전에 노트북에서 백업을 만듭니다.

```powershell
.\.venv\Scripts\python.exe -m scripts.backup_state
```

SQLite 백업 기능을 사용하므로 실행 중인 원본 DB를 그대로 복사하는 것보다 안전합니다.
다만 백업 이후 노트북에서 처리한 대화는 백업에 포함되지 않습니다. 최종 이전 때는 노트북의 새 요청 처리를 잠시 멈춘 상태에서 백업하고 전환합니다.
생성된 백업을 서버의 영구 볼륨에 `conversation-state.sqlite3`라는 이름으로 복원한 후 시작합니다.
기존 기억을 옮기지 않으면 서버에서 새 대화로 시작합니다. 복원 시 실행 중인 서버의 DB를 덮어쓰지 않습니다.

## 5. 카카오 웹훅 전환과 검증

1. 카카오 봇의 웹훅을 `https://발급된도메인/kakao`로 변경합니다.
2. 카카오톡에 메시지를 보내 `[연동 테스트] ...` 답장이 오는지 확인합니다.
3. Railway의 `LLM_MODE`를 `openai`로 변경하고 실제 답변과 대화 기억을 확인합니다.
4. 노트북의 Python 서버와 Cloudflare 터널을 끄고 다시 카카오톡 답장을 확인합니다.
5. Railway 서비스를 한 번 재시작한 뒤 이전 대화를 기억하는지 확인합니다.

## 운영 시 확인할 사항

- `/healthz`는 로컬 DB의 읽기·쓰기를 점검하며 OpenAI를 호출하지 않습니다. 점검용 기록은 롤백합니다.
- Railway의 배포 healthcheck는 새 버전 활성화 전 점검입니다. 이것만으로 운영 중 장애를 계속 감시하는 것은 아닙니다.
- `ALWAYS` 정책은 프로세스가 종료됐을 때 재시작합니다. 계정 잔액·서비스 장애·코드 오류까지 모두 해결해 주는 기능은 아닙니다.
- 최종 제출 후에는 심사 종료까지 코드·프롬프트·배포 상태를 변경하거나 삭제하지 않습니다. 필요한 배포와 검증은 제출 전에 마칩니다.
- 2026 예선 심사 운영 기한은 11월 1일 24:00(한국 시각)입니다. 계정 결제·크레딧·서버 운영 가능 기간을 그 이후까지 확보합니다.
- 정상 종료에는 대기 시간을 주지만, 갑작스런 강제 종료 시 메모리에서 처리 중이던 작업은 유실될 수 있습니다. 접수한 작업의 완전한 복구에는 영구 작업 큐가 추가로 필요합니다.
- 이전 응답 ID를 유지하려면 `/data`의 DB와 같은 OpenAI 프로젝트에 접근하는 API 키가 필요합니다.

실제 외부 서버 배포와 노트북 종료 후 동작 확인까지 마쳐야 이전이 완료된 것입니다.

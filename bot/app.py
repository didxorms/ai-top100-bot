import asyncio
import json
import os
import sqlite3
from pathlib import Path
from weakref import WeakValueDictionary

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse

from .conversation_state import ConversationStore, session_key_from_event
from .openai_payload import make_request_body, make_skill_response, read_answer
from .action_tools import PracticeActions, actor_key_from_event
from .tool_agent import run_tool_agent

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")
BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
# echo는 결제 없이 사용자 메시지 수신과 카카오 답장을 확인하는 모드입니다.
LLM_MODE = os.getenv("LLM_MODE", "openai").strip().lower()
WEB_SEARCH_MODE = os.getenv("WEB_SEARCH_MODE", "auto").strip().lower()
if WEB_SEARCH_MODE not in {"auto", "off"}:
    raise ValueError("WEB_SEARCH_MODE는 auto 또는 off여야 합니다.")
# 실제 수신 이벤트에서 질문이 d.content에 들어오는 것을 확인했습니다.
KAKAO_TEXT_PATH = os.getenv("KAKAO_TEXT_PATH", "").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", "").strip() or BASE_DIR / ".local")
if not DATA_DIR.is_absolute():
    DATA_DIR = BASE_DIR / DATA_DIR
STATE_STORE = ConversationStore(DATA_DIR / "conversation-state.sqlite3")
# 사용 중인 대화의 잠금만 유지합니다. 단일 Uvicorn 프로세스를 기준으로 합니다.
SESSION_LOCKS = WeakValueDictionary()
RESET_COMMANDS = {"/새대화", "/초기화", "/new"}
ACTION_MODE = os.getenv("ACTION_MODE", "off").strip().lower()
if ACTION_MODE not in {"off", "mock"}:
    raise ValueError("ACTION_MODE는 off 또는 mock이어야 합니다.")
PRACTICE_ACTIONS = PracticeActions(STATE_STORE.path) if ACTION_MODE == "mock" else None

app = FastAPI()


def event_shape(value):
    """필드 이름과 자료형만 남기고 모든 실제 값은 제외합니다."""
    if isinstance(value, dict):
        return {key: event_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [event_shape(item) for item in value[:1]]
    return type(value).__name__


def record_event_shape(event: dict):
    shape_file = DATA_DIR / "kakao-event-shape.json"
    shape_file.parent.mkdir(parents=True, exist_ok=True)
    shape_file.write_text(
        json.dumps(event_shape(event), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("수신 이벤트의 필드 구조 저장:", shape_file, flush=True)


def extract_user_text(event: dict, path: str):
    if not path:
        return None
    value = event
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit():
            index = int(key)
            value = value[index] if index < len(value) else None
        else:
            return None
    return value.strip() if isinstance(value, str) and value.strip() else None


async def generate_answer(user_text: str, session_key: str | None = None, actor_key: str | None = None):
    previous_id = STATE_STORE.get(session_key) if session_key else None
    # 질문과 연습용 도구 결과만 전달하며 카카오 이벤트, 식별자, 토큰은 제외합니다.
    async with httpx.AsyncClient(timeout=90) as client:
        body = make_request_body(
            user_text,
            model=OPENAI_MODEL,
            remember=session_key is not None,
            previous_response_id=previous_id,
            web_search=WEB_SEARCH_MODE == "auto",
        )
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        if PRACTICE_ACTIONS:
            result, answer = await run_tool_agent(client, body, headers, PRACTICE_ACTIONS, actor_key)
        else:
            response = await client.post(
                "https://api.openai.com/v1/responses", headers=headers, json=body,
            )
            response.raise_for_status()
            result = response.json()
            answer = read_answer(result)
            search_calls = sum(item.get("type") == "web_search_call" for item in result["output"])
            if search_calls:
                print("웹 검색 도구 사용:", search_calls, flush=True)
    if session_key:
        response_id = result.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise ValueError("대화를 이어가기 위한 OpenAI 응답 ID가 없습니다.")
        STATE_STORE.set(session_key, response_id)
    return answer


async def send_reply(callback_token: str, answer: str):

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://kapi.kakao.com/v1/bot/callback",
            headers={
                "Authorization": f"KakaoAK {BOT_TOKEN}",
                "X-Bot-Callback-Token": callback_token,
            },
            json={"skillResponse": make_skill_response(answer)},
        )

    print("콜백 HTTP 상태:", response.status_code, flush=True)
    response.raise_for_status()

    result = response.json()
    if not isinstance(result, dict):
        raise ValueError("카카오 콜백 응답 형식이 올바르지 않습니다.")
    print(
        "전송 성공:", result.get("sendCount"),
        "전송 실패:", result.get("errorCount"),
        flush=True,
    )
    if type(result.get("sendCount")) is not int or result["sendCount"] < 1 or result.get("errorCount") != 0:
        raise ValueError("카카오 메시지 전송에 실패했습니다.")


async def process_event(event: dict, callback_token: str, session_key: str | None):
    user_text = extract_user_text(event, KAKAO_TEXT_PATH)
    actor_key = actor_key_from_event(event) if PRACTICE_ACTIONS else None
    command_answer = None
    if user_text and PRACTICE_ACTIONS:
        command_answer = await PRACTICE_ACTIONS.handle_command(user_text, actor_key)
    if user_text is None:
        record_event_shape(event)
        print("KAKAO_TEXT_PATH 설정에 필요한 필드 구조를 저장했습니다.", flush=True)
        answer = "메시지를 받았습니다. 메시지 연결 설정을 확인 중입니다."
    elif command_answer is not None:
        answer = command_answer
    elif user_text in RESET_COMMANDS:
        if session_key:
            STATE_STORE.reset(session_key)
            if PRACTICE_ACTIONS and actor_key:
                await asyncio.to_thread(PRACTICE_ACTIONS.store.cancel, actor_key)
            answer = "새 대화를 시작했습니다. 다음 질문부터 이전 대화를 참고하지 않습니다."
        else:
            answer = "채팅방 정보를 확인하지 못해 새 대화를 시작할 수 없습니다."
    elif LLM_MODE == "echo":
        answer = f"[연동 테스트] {user_text}"
        print("메시지 수신 및 답장 테스트 (OpenAI 호출 없음)", flush=True)
    elif LLM_MODE != "openai":
        print("LLM_MODE를 echo 또는 openai로 설정해 주세요.", flush=True)
        answer = "응답 모드 설정을 확인해 주세요."
    elif not OPENAI_API_KEY:
        print(".env에 OPENAI_API_KEY를 입력한 뒤 서버를 재시작해 주세요.", flush=True)
        answer = "아직 OpenAI API 키가 설정되지 않았습니다."
    else:
        try:
            # 콜백 토큰의 유효 시간(5분)보다 짧게 전체 대기 시간을 제한합니다.
            if PRACTICE_ACTIONS:
                generation = generate_answer(user_text, session_key, actor_key)
            else:
                generation = generate_answer(user_text, session_key)
            answer = await asyncio.wait_for(generation, timeout=100)
            print("OpenAI 답변 생성 성공", flush=True)
        except httpx.HTTPStatusError as exc:
            print("OpenAI HTTP 오류:", exc.response.status_code, flush=True)
            answer = "답변을 생성하지 못했습니다. 잠시 후 다시 질문해 주세요."
        except (httpx.RequestError, TimeoutError, ValueError, sqlite3.Error) as exc:
            print("OpenAI 호출 실패:", type(exc).__name__, flush=True)
            answer = "답변을 생성하지 못했습니다. 잠시 후 다시 질문해 주세요."
    try:
        await send_reply(callback_token, answer)
    except (httpx.HTTPError, ValueError) as exc:
        print("카카오 답장 실패:", type(exc).__name__, flush=True)


async def handle_event(event: dict, callback_token: str):
    try:
        # DB 대기가 다른 웹훅의 1초 이내 접수 응답을 막지 않도록 합니다.
        accepted = await asyncio.to_thread(STATE_STORE.claim_callback, callback_token)
    except (OSError, sqlite3.Error) as exc:
        print("메시지 접수 상태 확인 실패:", type(exc).__name__, flush=True)
        try:
            await send_reply(callback_token, "메시지를 처리하지 못했습니다. 잠시 후 다시 질문해 주세요.")
        except (httpx.HTTPError, ValueError) as send_error:
            print("카카오 답장 실패:", type(send_error).__name__, flush=True)
        return
    if not accepted:
        print("중복 콜백 접수 생략", flush=True)
        return
    await handle_accepted_event(event, callback_token)


async def handle_accepted_event(event: dict, callback_token: str):
    session_key = session_key_from_event(event)
    if session_key is None:
        await process_event(event, callback_token, None)
        return
    lock = SESSION_LOCKS.get(session_key)
    if lock is None:
        lock = asyncio.Lock()
        SESSION_LOCKS[session_key] = lock
    try:
        # 같은 방의 요청을 순서대로 처리하되, 콜백 유효 시간을 넘겨 기다리지 않습니다.
        await asyncio.wait_for(lock.acquire(), timeout=110)
    except TimeoutError:
        try:
            await send_reply(callback_token, "앞선 질문을 처리 중입니다. 잠시 후 다시 질문해 주세요.")
        except (httpx.HTTPError, ValueError) as exc:
            print("카카오 답장 실패:", type(exc).__name__, flush=True)
        return
    try:
        await process_event(event, callback_token, session_key)
    except (OSError, sqlite3.Error) as exc:
        print("대화 상태 처리 실패:", type(exc).__name__, flush=True)
        try:
            await send_reply(callback_token, "대화 상태를 확인하지 못했습니다. 잠시 후 다시 질문해 주세요.")
        except (httpx.HTTPError, ValueError) as send_error:
            print("카카오 답장 실패:", type(send_error).__name__, flush=True)
    finally:
        lock.release()


@app.get("/")
async def health():
    return {"status": "ok"}


@app.get("/healthz")
async def readiness():
    try:
        # DB 처리가 웹훅의 접수 응답을 막지 않도록 별도 스레드에서 점검합니다.
        await asyncio.to_thread(STATE_STORE.check_health)
    except (OSError, sqlite3.Error) as exc:
        print("저장 공간 상태 점검 실패:", type(exc).__name__, flush=True)
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}


@app.post("/kakao")
async def receive_event(event: dict, tasks: BackgroundTasks):
    data = event.get("d")
    if isinstance(data, dict):
        callback_token = data.get("callbackToken")
        if isinstance(callback_token, str) and callback_token.strip():
            tasks.add_task(handle_event, event, callback_token)

    # 응답을 먼저 보낸 뒤 LLM 처리와 답장 전송을 시작합니다.
    return {"ok": True}

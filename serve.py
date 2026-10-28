"""로컬과 클라우드에서 같은 설정으로 실행하는 서버 진입점."""

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


def server_options():
    return {
        "host": "0.0.0.0",
        "port": int(os.getenv("PORT", "8000")),
        # SQLite와 채팅방별 잠금은 단일 프로세스를 기준으로 합니다.
        "workers": 1,
        "timeout_graceful_shutdown": 270,
    }


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    uvicorn.run("bot.app:app", **server_options())

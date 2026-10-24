"""실행 중인 SQLite DB를 안전하게 백업합니다. 외부 서버에는 전송하지 않습니다."""

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


def backup_database(source: Path, destination: Path):
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise FileNotFoundError("백업할 대화 상태 DB가 없습니다.")
    if source == destination or destination.exists():
        raise FileExistsError("기존 파일을 덮어쓰지 않도록 다른 백업 경로를 지정해 주세요.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
    finally:
        source_connection.close()


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")
    data_dir = Path(os.getenv("DATA_DIR", "").strip() or base_dir / ".local")
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=base_dir / ".local" / "backups" / f"conversation-state-{stamp}.sqlite3")
    options = parser.parse_args()
    backup_database(data_dir / "conversation-state.sqlite3", options.output)
    print("백업 완료:", options.output.resolve())

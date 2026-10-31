"""Build a private deployment bundle; output must never be committed."""

import base64
import json
from pathlib import Path
import re
import zlib

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / ".local/contest/docs"
    documents = {}
    for path in sorted(folder.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        # Old discovery to-dos are interview notes, not customer-facing policy.
        text = re.split(r"(?m)^## (?:후속|활용 및 후속|미해결 상충과 추가 자료)", text)[0]
        text = re.sub(r"(?m)^.*(?:후속문서:|후속자료:|新참조|新資料|新자료:|新人物|新문서).*$", "", text)
        key = path.stem
        documents[key] = {"title": text.splitlines()[0].lstrip("# "), "source": path.name, "text": text}
    for index, path in enumerate(sorted(folder.rglob("*extracted.txt"))):
        text = path.read_text(encoding="utf-8")
        if text.strip():
            documents[f"original_{index:02}"] = {"title": path.stem.replace("-extracted", "") + " 원문",
                "source": str(path.relative_to(folder)), "text": text}
    # Preserve separately recorded oral testimony without letting it override documentary originals.
    interviews = []
    for path in sorted((ROOT / ".local/contest/interviews").glob("*.md")):
        interviews.append(path.stem + "\n" + path.read_text(encoding="utf-8"))
    documents["interview-records"] = {"title": "관계자 인터뷰 기록(구술·정정·미확인 구별, 원문 우선)",
        "source": "interviews", "text": "\n\n".join(interviews)}
    calendar = json.loads((folder / "business-calendar/calendar-dates.json").read_text(encoding="utf-8"))
    bundle = {"version": 1, "documents": documents, "calendar": calendar,
              "instructions": (ROOT / ".local/contest/runtime-instructions.txt").read_text(encoding="utf-8"),
              "public_context": (ROOT / ".local/contest/public-context.txt").read_text(encoding="utf-8"),
              "api": json.loads((ROOT / "TC/actions.json").read_text(encoding="utf-8"))}
    payload = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    output = ROOT / ".local/contest/runtime-bundle.json"
    output.write_text(payload, encoding="utf-8")
    encoded = base64.b64encode(zlib.compress(payload.encode(), 9)).decode()
    (output.parent / "runtime-bundle.b64").write_text(encoded, encoding="ascii")
    print(f"Private bundle: {len(documents)} documents, {len(encoded)} encoded bytes")


if __name__ == "__main__":
    main()

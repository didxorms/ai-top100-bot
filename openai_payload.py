"""OpenAI 연동용 요청과 응답 형식만 준비합니다. 외부 API는 호출하지 않습니다."""


def make_request_body(
    user_text: str,
    model: str = "gpt-4.1-mini",
    *,
    remember: bool = False,
    previous_response_id: str | None = None,
):
    if previous_response_id and not remember:
        raise ValueError("이전 응답을 이어가려면 remember=True가 필요합니다.")
    body = {
        "model": model,
        "instructions": (
            "당신은 카카오톡에서 질문에 답하는 도우미입니다. "
            "한국어로 명확하고 간결하게, 가능하면 800자 이내로 답하세요."
        ),
        "input": user_text,
        "max_output_tokens": 700,
        "store": remember,
    }
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    return body


def read_answer(response: dict):
    texts = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                texts.append(part["text"])
            elif part.get("type") == "refusal":
                texts.append(part["refusal"])
    answer = "\n".join(texts).strip()
    if not answer:
        raise ValueError("OpenAI 응답에 텍스트가 없습니다.")
    return answer


def make_skill_response(answer: str):
    # 카카오 제한: 말풍선당 1,000자, 최대 3개 말풍선.
    answer = answer.strip() or "답변을 생성하지 못했습니다. 다시 질문해 주세요."
    if len(answer) > 3000:
        answer = answer[:2999] + "…"
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": answer[start:start + 1000]}}
                for start in range(0, len(answer), 1000)
            ]
        },
    }

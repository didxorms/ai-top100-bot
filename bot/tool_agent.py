"""Responses API의 함수 요청을 검증하고 결과를 돌려주는 제한된 실행 루프."""

import json

from .action_tools import ACTION_INSTRUCTIONS, TOOLS
from .openai_payload import read_answer


async def run_tool_agent(client, body, headers, actions, actor):
    body = {**body, "instructions": body["instructions"] + ACTION_INSTRUCTIONS,
            "tools": [*body.get("tools", []), *TOOLS], "parallel_tool_calls": False}
    history = [{"role": "user", "content": body["input"]}]
    calls_used = 0
    searches_left = body.get("max_tool_calls", 0)
    for round_number in range(5):
        if round_number == 4 or calls_used >= 4:
            body["tool_choice"] = "none"
        response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=body)
        response.raise_for_status()
        result = response.json()
        if result.get("status", "completed") != "completed":
            raise ValueError("미완료 응답은 대화 상태로 저장하지 않습니다.")
        output = result.get("output", [])
        searches = sum(item.get("type") == "web_search_call" for item in output)
        if searches:
            print("웹 검색 도구 사용:", searches, flush=True)
            searches_left -= searches
            if searches_left < 0:
                raise ValueError("허용된 검색 횟수를 넘었습니다.")
            if searches_left:
                body["max_tool_calls"] = searches_left
            else:
                body["tools"] = [tool for tool in body["tools"] if tool.get("type") != "web_search"]
                body.pop("max_tool_calls", None)
        calls = [item for item in output if item.get("type") == "function_call"]
        if not calls:
            answer = read_answer(result)
            hint = await actions.pending_hint(actor)
            # 카카오의 전체 3,000자 제한 안에서 서버가 만든 확인 안내를 보장합니다.
            return result, answer[:3000 - len(hint)] + hint
        if body.get("tool_choice") == "none" or len(calls) + calls_used > 4:
            raise ValueError("허용된 도구 호출 횟수를 넘었습니다.")
        outputs = []
        for call in calls:
            call_id = call.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("도구 요청에 call_id가 없습니다.")
            tool_result = await actions.execute(call.get("name"), call.get("arguments"), actor)
            outputs.append({"type": "function_call_output", "call_id": call_id,
                            "output": json.dumps(tool_result, ensure_ascii=False)})
            calls_used += 1
        if body.get("store"):
            response_id = result.get("id")
            if not isinstance(response_id, str) or not response_id:
                raise ValueError("도구 결과를 연결할 응답 ID가 없습니다.")
            body = {**body, "previous_response_id": response_id, "input": outputs}
        else:
            # 저장하지 않는 대화에서도 함수 요청과 결과를 함께 다시 전달합니다.
            history.extend(output)
            history.extend(outputs)
            body = {**body, "input": list(history)}
    raise ValueError("도구 실행을 마무리하지 못했습니다.")

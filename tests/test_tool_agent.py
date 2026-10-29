import json
import tempfile
import unittest
from pathlib import Path

import httpx

from bot.action_tools import PracticeActions
from bot.openai_payload import make_request_body
from bot.tool_agent import run_tool_agent


def function_call(name="list_events", arguments="{}", call_id="call_1"):
    return {"type": "function_call", "name": name, "arguments": arguments, "call_id": call_id}


def response(response_id, output, status="completed"):
    return httpx.Response(200, json={"id": response_id, "status": status, "output": output})


def text_output(text="연습용 행사 목록입니다."):
    return [{"type": "message", "content": [{"type": "output_text", "text": text}]}]


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.actions = PracticeActions(Path(directory.name) / "state.sqlite3")

    async def run_agent(self, handler, remember=True, actor="private-actor"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_tool_agent(client, make_request_body("행사 알려줘", remember=remember),
                                        {"Authorization": "Bearer fake-openai"}, self.actions, actor)

    async def test_function_result_is_linked_by_response_and_call_id_without_actor(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return response("resp_tool", [function_call()])
            return response("resp_final", text_output())

        result, answer = await self.run_agent(handler)
        self.assertEqual(result["id"], "resp_final")
        self.assertIn("행사", answer)
        self.assertEqual(requests[1]["previous_response_id"], "resp_tool")
        self.assertEqual(requests[1]["input"][0]["call_id"], "call_1")
        self.assertIn("EVT001", requests[1]["input"][0]["output"])
        self.assertNotIn("private-actor", json.dumps(requests))
        self.assertTrue(all(tool["strict"] for tool in requests[0]["tools"]))

    async def test_stateless_calls_replay_function_items_and_outputs(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return (response("resp_tool", [function_call()]) if len(requests) == 1
                    else response("resp_final", text_output()))

        await self.run_agent(handler, remember=False)
        self.assertNotIn("previous_response_id", requests[1])
        self.assertEqual([item.get("type") for item in requests[1]["input"]],
                         [None, "function_call", "function_call_output"])

    async def test_model_can_prepare_but_cannot_confirm_and_hint_is_server_generated(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return response("resp_prepare", [function_call("prepare_registration",
                                '{"event_id":"EVT001","quantity":2}')])
            return response("resp_final", text_output("신청 내용을 준비했습니다."))

        _, answer = await self.run_agent(handler)
        self.assertIn("/신청확정 ", answer)
        self.assertIn("실제 예약이나 결제는 발생하지 않습니다", answer)
        self.assertEqual(self.actions.store.registrations("private-actor"), [])

    async def test_forged_confirm_tool_gets_an_error_and_cannot_write(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return response("resp_bad", [function_call("confirm_registration")])
            return response("resp_final", text_output("직접 확인이 필요합니다."))

        await self.run_agent(handler)
        self.assertIn("unsupported_tool", requests[1]["input"][0]["output"])
        self.assertEqual(self.actions.store.registrations("private-actor"), [])

    async def test_repeated_tool_calls_are_bounded(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return response(f"resp_{len(requests)}", [function_call(call_id=f"call_{len(requests)}")])

        with self.assertRaises(ValueError):
            await self.run_agent(handler)
        self.assertEqual(len(requests), 5)
        self.assertEqual(requests[-1]["tool_choice"], "none")

    async def test_incomplete_response_cannot_be_saved_as_success(self):
        with self.assertRaises(ValueError):
            await self.run_agent(lambda request: response("resp_partial", text_output(), status="incomplete"))

    async def test_http_failure_is_propagated(self):
        with self.assertRaises(httpx.HTTPStatusError):
            await self.run_agent(lambda request: httpx.Response(503))

    async def test_web_search_is_preserved_with_actions_and_budget_spans_rounds(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) < 3:
                return response(f"resp_{len(requests)}", [
                    {"type": "web_search_call", "status": "completed"},
                    function_call(call_id=f"call_{len(requests)}"),
                ])
            return response("resp_final", text_output())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await run_tool_agent(client, make_request_body("행사와 뉴스", web_search=True),
                                 {"Authorization": "Bearer fake"}, self.actions, "actor")
        self.assertEqual([r["max_tool_calls"] for r in requests], [3, 2, 1])
        self.assertTrue(all(any(tool["type"] == "web_search" for tool in r["tools"]) for r in requests))
        self.assertTrue(all(any(tool.get("name") == "list_events" for tool in r["tools"]) for r in requests))

    async def test_search_budget_exhaustion_disables_only_search(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return response("resp_first", [{"type": "web_search_call", "status": "completed"}] * 3
                                + [function_call()])
            return response("resp_final", text_output())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await run_tool_agent(client, make_request_body("행사", web_search=True),
                                 {"Authorization": "Bearer fake"}, self.actions, "actor")
        self.assertNotIn("max_tool_calls", requests[1])
        self.assertTrue(all(tool["type"] == "function" for tool in requests[1]["tools"]))


if __name__ == "__main__":
    unittest.main()

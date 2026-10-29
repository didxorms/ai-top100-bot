import asyncio
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import BackgroundTasks
from bot.conversation_state import ConversationStore, session_key_from_event
from bot.action_tools import PracticeActions, actor_key_from_event

# 실제 .env의 인증 정보는 테스트에 사용하지 않습니다.
with patch.dict(os.environ, {
    "BOT_TOKEN": "fake-bot-secret",
    "OPENAI_API_KEY": "fake-openai-secret",
    "OPENAI_MODEL": "test-model",
    "LLM_MODE": "openai",
    "KAKAO_TEXT_PATH": "d.content",
    "ACTION_MODE": "off",
}):
    from bot import app as bot

REAL_ASYNC_CLIENT = httpx.AsyncClient


class AppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_directory.cleanup)
        self.store = ConversationStore(Path(self.state_directory.name) / "state.sqlite3")
        store_patch = patch("bot.app.STATE_STORE", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)

    async def test_question_to_openai_and_answer_to_kakao(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == "api.openai.com":
                return httpx.Response(200, json={"output": [
                    {"type": "reasoning"},
                    {"type": "message", "content": [
                        {"type": "output_text", "text": "테스트 답변"}
                    ]},
                ]})
            if request.url.host == "kapi.kakao.com":
                return httpx.Response(200, json={"sendCount": 1, "errorCount": 0})
            raise AssertionError("예상하지 않은 외부 주소")

        def client_factory(**kwargs):
            return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        event = {"d": {"callbackToken": "fake-callback-secret", "content": "테스트 질문"}}
        with patch("bot.app.httpx.AsyncClient", side_effect=client_factory):
            await bot.handle_event(event, "fake-callback-secret")

        self.assertEqual(len(requests), 2)
        outgoing = requests[0]
        body = json.loads(outgoing.content)
        self.assertEqual(body["input"], "테스트 질문")
        self.assertEqual(outgoing.headers["Authorization"], "Bearer fake-openai-secret")
        self.assertNotIn("fake-bot-secret", outgoing.content.decode())
        self.assertNotIn("fake-callback-secret", outgoing.content.decode())
        self.assertNotIn("X-Bot-Callback-Token", outgoing.headers)
        callback = requests[1]
        self.assertEqual(callback.headers["X-Bot-Callback-Token"], "fake-callback-secret")
        self.assertEqual(callback.headers["Authorization"], "KakaoAK fake-bot-secret")
        self.assertEqual(
            json.loads(callback.content)["skillResponse"]["template"]["outputs"][0]["simpleText"]["text"],
            "테스트 답변",
        )

    async def test_receipt_returns_before_llm_work(self):
        tasks = BackgroundTasks()
        with patch("bot.app.handle_event", new_callable=AsyncMock) as handler:
            response = await bot.receive_event({"d": {"callbackToken": "fake-token"}}, tasks)
            self.assertEqual(response, {"ok": True})
            self.assertEqual(len(tasks.tasks), 1)
            handler.assert_not_awaited()

    async def test_unknown_question_path_does_not_send_event_to_openai(self):
        with patch("bot.app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("bot.app.send_reply", new_callable=AsyncMock), \
             patch("bot.app.record_event_shape") as record:
            await bot.handle_event({"d": {"callbackToken": "fake-token"}}, "fake-token")
            generate.assert_not_awaited()
            record.assert_called_once()

    async def test_openai_auth_error_still_returns_a_safe_kakao_reply(self):
        error = httpx.HTTPStatusError(
            "private error details", request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
            response=httpx.Response(401),
        )
        with patch("bot.app.generate_answer", new_callable=AsyncMock, side_effect=error), \
             patch("bot.app.send_reply", new_callable=AsyncMock) as reply:
            await bot.handle_event({"d": {"content": "테스트 질문"}}, "fake-token")
            reply.assert_awaited_once()
            self.assertNotIn("private error details", reply.await_args.args[1])

    async def test_missing_key_does_not_attempt_openai(self):
        with patch("bot.app.OPENAI_API_KEY", ""), \
             patch("bot.app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("bot.app.send_reply", new_callable=AsyncMock):
            await bot.handle_event({"d": {"content": "테스트 질문"}}, "fake-token")
            generate.assert_not_awaited()

    async def test_shape_file_excludes_message_and_tokens(self):
        with tempfile.TemporaryDirectory() as directory, patch("bot.app.DATA_DIR", Path(directory)):
            bot.record_event_shape({"d": {
                "callbackToken": "sensitive-token-value",
                "testQuestion": "private-message-value",
                "user": {"id": "private-user-id"},
            }})
            saved = (Path(directory) / "kakao-event-shape.json").read_text(encoding="utf-8")
            for private_value in ("sensitive-token-value", "private-message-value", "private-user-id"):
                self.assertNotIn(private_value, saved)
            self.assertEqual(json.loads(saved)["d"]["testQuestion"], "str")

    async def test_echo_mode_returns_actual_content_without_openai(self):
        with patch("bot.app.LLM_MODE", "echo"), \
             patch("bot.app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("bot.app.send_reply", new_callable=AsyncMock) as reply:
            await bot.handle_event({"d": {"content": "안녕"}}, "fake-token")
            generate.assert_not_awaited()
            reply.assert_awaited_once_with("fake-token", "[연동 테스트] 안녕")

    async def test_invalid_mode_does_not_accidentally_call_openai(self):
        with patch("bot.app.LLM_MODE", "typo"), \
             patch("bot.app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("bot.app.send_reply", new_callable=AsyncMock):
            await bot.handle_event({"d": {"content": "안녕"}}, "fake-token")
            generate.assert_not_awaited()

    async def test_invalid_event_data_is_ignored(self):
        for event in ({}, {"d": None}, {"d": "invalid"}, {"d": {"callbackToken": 123}}):
            with self.subTest(event=event):
                tasks = BackgroundTasks()
                self.assertEqual(await bot.receive_event(event, tasks), {"ok": True})
                self.assertEqual(tasks.tasks, [])

    async def test_two_turns_continue_after_store_reopen(self):
        requests = []
        counter = 0

        def handler(request):
            nonlocal counter
            if request.url.host == "api.openai.com":
                counter += 1
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={
                    "id": f"resp_{counter}",
                    "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": f"테스트 답변 {counter}"}
                    ]}],
                })
            if request.url.host == "kapi.kakao.com":
                return httpx.Response(200, json={"sendCount": 1, "errorCount": 0})
            raise AssertionError("예상하지 않은 외부 주소")

        def client_factory(**kwargs):
            return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        event = {"d": {
            "isChannelChatroom": True, "botUserKey": "test-user", "content": "예산은 10만 원이야",
        }}
        with patch("bot.app.httpx.AsyncClient", side_effect=client_factory):
            await bot.handle_event(event, "fake-callback-first")
            # 프로세스 재시작 시처럼 저장소 객체를 새로 만들어도 같은 응답 ID를 사용합니다.
            reopened = ConversationStore(self.store.path)
            with patch("bot.app.STATE_STORE", reopened):
                await bot.handle_event({"d": {**event["d"], "content": "예산이 얼마였지?"}}, "fake-callback-second")

        self.assertEqual(len(requests), 2)
        self.assertNotIn("previous_response_id", requests[0])
        self.assertEqual(requests[1]["previous_response_id"], "resp_1")
        self.assertTrue(all(request["store"] for request in requests))
        self.assertEqual(requests[1]["input"], "예산이 얼마였지?")
        self.assertNotIn("test-user", json.dumps(requests))
        self.assertEqual(self.store.get(session_key_from_event(event)), "resp_2")

    async def test_new_chat_resets_only_the_current_room_without_model_call(self):
        event = {"d": {"botGroupKey": "room-one", "content": "/새대화"}}
        session_key = session_key_from_event(event)
        self.store.set(session_key, "resp_old")
        self.store.set("another-session", "resp_keep")
        with patch("bot.app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("bot.app.send_reply", new_callable=AsyncMock) as reply:
            await bot.handle_event(event, "fake-token")
        generate.assert_not_awaited()
        self.assertIsNone(self.store.get(session_key))
        self.assertEqual(self.store.get("another-session"), "resp_keep")
        self.assertIn("새 대화", reply.await_args.args[1])

    async def test_same_room_is_processed_in_order(self):
        order = []

        async def generate(question, session_key):
            order.append(f"{question}:start")
            await asyncio.sleep(0)
            order.append(f"{question}:end")
            return "테스트 답변"

        first = {"d": {"botGroupKey": "shared-room", "botUserKey": "user-a", "content": "first"}}
        second = {"d": {"botGroupKey": "shared-room", "botUserKey": "user-b", "content": "second"}}
        with patch("bot.app.generate_answer", side_effect=generate), \
             patch("bot.app.send_reply", new_callable=AsyncMock):
            await asyncio.gather(bot.handle_event(first, "fake-one"), bot.handle_event(second, "fake-two"))
        self.assertEqual(order, ["first:start", "first:end", "second:start", "second:end"])

    async def test_failed_generation_does_not_replace_previous_id(self):
        self.store.set("session", "resp_keep")

        def handler(request):
            return httpx.Response(503, json={"error": {"message": "temporary failure"}})

        def client_factory(**kwargs):
            return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        with patch("bot.app.httpx.AsyncClient", side_effect=client_factory):
            with self.assertRaises(httpx.HTTPStatusError):
                await bot.generate_answer("질문", "session")
        self.assertEqual(self.store.get("session"), "resp_keep")

    async def test_readiness_checks_storage_without_calling_external_apis(self):
        self.store.set("session", "resp_keep")
        with patch("bot.app.generate_answer", new_callable=AsyncMock) as generate:
            self.assertEqual(await bot.readiness(), {"status": "ok"})
            generate.assert_not_awaited()
        self.assertEqual(self.store.get("session"), "resp_keep")
        self.assertIsNone(self.store.get("__healthcheck__"))

    async def test_readiness_fails_when_storage_is_unavailable(self):
        with patch.object(self.store, "check_health", side_effect=OSError("private filesystem details")):
            response = await bot.readiness()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private filesystem details", response.body.decode())

    async def test_mock_actions_full_question_prepare_and_direct_confirmation_flow(self):
        actions = PracticeActions(self.store.path)
        openai_requests = []
        replies = []

        def handler(request):
            if request.url.host == "api.openai.com":
                openai_requests.append(json.loads(request.content))
                if len(openai_requests) == 1:
                    return httpx.Response(200, json={"id": "resp_prepare", "output": [{
                        "type": "function_call", "name": "prepare_registration", "call_id": "call_prepare",
                        "arguments": '{"event_id":"EVT001","quantity":2}',
                    }]})
                return httpx.Response(200, json={"id": "resp_done", "output": [{
                    "type": "message", "content": [{"type": "output_text", "text": "신청 내용을 준비했어요."}],
                }]})
            if request.url.host == "kapi.kakao.com":
                replies.append(json.loads(request.content))
                return httpx.Response(200, json={"sendCount": 1, "errorCount": 0})
            raise AssertionError("예상하지 않은 외부 주소")

        def client_factory(**kwargs):
            if "transport" not in kwargs:
                kwargs["transport"] = httpx.MockTransport(handler)
            return REAL_ASYNC_CLIENT(**kwargs)

        event = {"d": {"isChannelChatroom": True, "botUserKey": "fake-private-user",
                       "content": "EVT001에 2명 신청해줘"}}
        actor = actor_key_from_event(event)
        with patch("bot.app.PRACTICE_ACTIONS", actions), \
             patch("bot.app.httpx.AsyncClient", side_effect=client_factory):
            await bot.handle_event(event, "fake-callback")
            self.assertEqual(actions.store.registrations(actor), [])
            pending = actions.store.pending(actor)
            self.assertIn(pending["code"], json.dumps(replies, ensure_ascii=False))
            await bot.handle_event({"d": {**event["d"], "content": "/신청확정 " + pending["code"]}}, "fake-confirm-callback")
            self.assertEqual(len(actions.store.registrations(actor)), 1)
            self.assertEqual(len(openai_requests), 2)  # 확정은 LLM 호출 없이 서버가 처리합니다.
            self.assertEqual(self.store.get(session_key_from_event(event)), "resp_done")
        outgoing = json.dumps(openai_requests)
        for secret in ("fake-private-user", "fake-callback", "fake-bot-secret", actor):
            self.assertNotIn(secret, outgoing)

    async def test_practice_help_does_not_call_openai(self):
        actions = PracticeActions(self.store.path)
        with patch("bot.app.PRACTICE_ACTIONS", actions), \
             patch("bot.app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("bot.app.send_reply", new_callable=AsyncMock) as reply:
            await bot.handle_event({"d": {"content": "/실습"}}, "fake-token")
        generate.assert_not_awaited()
        self.assertIn("실습", reply.await_args.args[1])

    async def test_duplicate_delivery_is_processed_once_even_after_store_reopen(self):
        event = {"d": {"botGroupKey": "room", "content": "테스트 질문"}}
        with patch("bot.app.generate_answer", new_callable=AsyncMock, return_value="답변") as generate, \
             patch("bot.app.send_reply", new_callable=AsyncMock) as reply:
            await asyncio.gather(*(bot.handle_event(event, "same-callback") for _ in range(3)))
            with patch("bot.app.STATE_STORE", ConversationStore(self.store.path)):
                await bot.handle_event(event, "same-callback")
            generate.assert_awaited_once()
            reply.assert_awaited_once()

    async def test_same_text_with_distinct_callback_tokens_is_not_deduplicated(self):
        event = {"d": {"content": "같은 질문"}}
        with patch("bot.app.generate_answer", new_callable=AsyncMock, return_value="답변") as generate, \
             patch("bot.app.send_reply", new_callable=AsyncMock):
            await bot.handle_event(event, "callback-one")
            await bot.handle_event(event, "callback-two")
            self.assertEqual(generate.await_count, 2)

    async def test_incomplete_generation_does_not_replace_previous_id(self):
        self.store.set("session", "resp_keep")

        def handler(request):
            return httpx.Response(200, json={"id": "resp_partial", "status": "incomplete", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "미완성 답변"}]},
            ]})

        def client_factory(**kwargs):
            return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        with patch("bot.app.httpx.AsyncClient", side_effect=client_factory):
            with self.assertRaises(ValueError):
                await bot.generate_answer("질문", "session")
        self.assertEqual(self.store.get("session"), "resp_keep")

    async def test_rate_limit_and_timeout_return_safe_reply_without_logging_secrets(self):
        errors = [TimeoutError("private-secret"), httpx.HTTPStatusError(
            "private-secret", request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
            response=httpx.Response(429),
        )]
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__), \
                 patch("bot.app.generate_answer", new_callable=AsyncMock, side_effect=error), \
                 patch("bot.app.send_reply", new_callable=AsyncMock) as reply, \
                 redirect_stdout(StringIO()) as logs:
                await bot.handle_event({"d": {"content": "질문"}}, f"failure-callback-{index}")
                reply.assert_awaited_once()
                self.assertNotIn("private-secret", logs.getvalue() + reply.await_args.args[1])

    async def test_callback_http_success_with_delivery_failure_is_detected(self):
        def client_factory(**kwargs):
            return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"sendCount": 0, "errorCount": 1})
            ), **kwargs)

        with patch("bot.app.httpx.AsyncClient", side_effect=client_factory):
            with self.assertRaises(ValueError):
                await bot.send_reply("fake-callback", "테스트 답변")

    async def test_malformed_callback_response_does_not_crash_background_handler(self):
        def client_factory(**kwargs):
            return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=[])
            ), **kwargs)

        with patch("bot.app.generate_answer", new_callable=AsyncMock, return_value="답변"), \
             patch("bot.app.httpx.AsyncClient", side_effect=client_factory):
            await bot.handle_event({"d": {"content": "질문"}}, "malformed-callback")


if __name__ == "__main__":
    unittest.main()

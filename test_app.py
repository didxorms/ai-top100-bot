import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import BackgroundTasks
from conversation_state import ConversationStore, session_key_from_event

# 실제 .env의 인증 정보는 테스트에 사용하지 않습니다.
with patch.dict(os.environ, {
    "BOT_TOKEN": "fake-bot-secret",
    "OPENAI_API_KEY": "fake-openai-secret",
    "OPENAI_MODEL": "test-model",
    "LLM_MODE": "openai",
    "KAKAO_TEXT_PATH": "d.content",
}):
    import app as bot

REAL_ASYNC_CLIENT = httpx.AsyncClient


class AppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_directory.cleanup)
        self.store = ConversationStore(Path(self.state_directory.name) / "state.sqlite3")
        store_patch = patch("app.STATE_STORE", self.store)
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
        with patch("app.httpx.AsyncClient", side_effect=client_factory):
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
        with patch("app.handle_event", new_callable=AsyncMock) as handler:
            response = await bot.receive_event({"d": {"callbackToken": "fake-token"}}, tasks)
            self.assertEqual(response, {"ok": True})
            self.assertEqual(len(tasks.tasks), 1)
            handler.assert_not_awaited()

    async def test_unknown_question_path_does_not_send_event_to_openai(self):
        with patch("app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("app.send_reply", new_callable=AsyncMock), \
             patch("app.record_event_shape") as record:
            await bot.handle_event({"d": {"callbackToken": "fake-token"}}, "fake-token")
            generate.assert_not_awaited()
            record.assert_called_once()

    async def test_openai_auth_error_still_returns_a_safe_kakao_reply(self):
        error = httpx.HTTPStatusError(
            "private error details", request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
            response=httpx.Response(401),
        )
        with patch("app.generate_answer", new_callable=AsyncMock, side_effect=error), \
             patch("app.send_reply", new_callable=AsyncMock) as reply:
            await bot.handle_event({"d": {"content": "테스트 질문"}}, "fake-token")
            reply.assert_awaited_once()
            self.assertNotIn("private error details", reply.await_args.args[1])

    async def test_missing_key_does_not_attempt_openai(self):
        with patch("app.OPENAI_API_KEY", ""), \
             patch("app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("app.send_reply", new_callable=AsyncMock):
            await bot.handle_event({"d": {"content": "테스트 질문"}}, "fake-token")
            generate.assert_not_awaited()

    async def test_shape_file_excludes_message_and_tokens(self):
        with tempfile.TemporaryDirectory() as directory, patch("app.DATA_DIR", Path(directory)):
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
        with patch("app.LLM_MODE", "echo"), \
             patch("app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("app.send_reply", new_callable=AsyncMock) as reply:
            await bot.handle_event({"d": {"content": "안녕"}}, "fake-token")
            generate.assert_not_awaited()
            reply.assert_awaited_once_with("fake-token", "[연동 테스트] 안녕")

    async def test_invalid_mode_does_not_accidentally_call_openai(self):
        with patch("app.LLM_MODE", "typo"), \
             patch("app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("app.send_reply", new_callable=AsyncMock):
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
        with patch("app.httpx.AsyncClient", side_effect=client_factory):
            await bot.handle_event(event, "fake-callback-first")
            # 프로세스 재시작 시처럼 저장소 객체를 새로 만들어도 같은 응답 ID를 사용합니다.
            reopened = ConversationStore(self.store.path)
            with patch("app.STATE_STORE", reopened):
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
        with patch("app.generate_answer", new_callable=AsyncMock) as generate, \
             patch("app.send_reply", new_callable=AsyncMock) as reply:
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
        with patch("app.generate_answer", side_effect=generate), \
             patch("app.send_reply", new_callable=AsyncMock):
            await asyncio.gather(bot.handle_event(first, "fake-one"), bot.handle_event(second, "fake-two"))
        self.assertEqual(order, ["first:start", "first:end", "second:start", "second:end"])

    async def test_failed_generation_does_not_replace_previous_id(self):
        self.store.set("session", "resp_keep")

        def handler(request):
            return httpx.Response(503, json={"error": {"message": "temporary failure"}})

        def client_factory(**kwargs):
            return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        with patch("app.httpx.AsyncClient", side_effect=client_factory):
            with self.assertRaises(httpx.HTTPStatusError):
                await bot.generate_answer("질문", "session")
        self.assertEqual(self.store.get("session"), "resp_keep")

    async def test_readiness_checks_storage_without_calling_external_apis(self):
        self.store.set("session", "resp_keep")
        with patch("app.generate_answer", new_callable=AsyncMock) as generate:
            self.assertEqual(await bot.readiness(), {"status": "ok"})
            generate.assert_not_awaited()
        self.assertEqual(self.store.get("session"), "resp_keep")
        self.assertIsNone(self.store.get("__healthcheck__"))

    async def test_readiness_fails_when_storage_is_unavailable(self):
        with patch.object(self.store, "check_health", side_effect=OSError("private filesystem details")):
            response = await bot.readiness()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private filesystem details", response.body.decode())


if __name__ == "__main__":
    unittest.main()

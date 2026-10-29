import unittest

from bot.openai_payload import make_request_body, make_skill_response, read_answer


class PayloadTests(unittest.TestCase):
    def test_reasoning_before_text_and_multiple_text_parts(self):
        response = {
            "output": [
                {"type": "reasoning"},
                {"type": "message", "content": [
                    {"type": "output_text", "text": "첫 답변"},
                    {"type": "output_text", "text": "다음 답변"},
                ]},
            ]
        }
        self.assertEqual(read_answer(response), "첫 답변\n다음 답변")

    def test_refusal_is_displayable(self):
        response = {"output": [{"type": "message", "content": [
            {"type": "refusal", "refusal": "해당 요청은 도와드릴 수 없습니다."}
        ]}]}
        self.assertEqual(read_answer(response), "해당 요청은 도와드릴 수 없습니다.")

    def test_empty_response_does_not_become_a_successful_answer(self):
        with self.assertRaises(ValueError):
            read_answer({"output": [{"type": "reasoning"}]})

    def test_incomplete_and_malformed_responses_are_rejected(self):
        for response in (
            {"status": "incomplete", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "partial"}
            ]}]},
            None, [], {"output": None}, {"output": [None]},
            {"output": [{"type": "message", "content": None}]},
            {"output": [{"type": "message", "content": [None]}]},
            {"output": [{"type": "message", "content": [{"type": "output_text", "text": 1}]}]},
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                read_answer(response)

    def test_kakao_limits_and_no_loss_within_limit(self):
        for length in (1, 1000, 1001, 3000, 3001):
            with self.subTest(length=length):
                answer = "가" * length
                outputs = make_skill_response(answer)["template"]["outputs"]
                self.assertTrue(1 <= len(outputs) <= 3)
                chunks = [output["simpleText"]["text"] for output in outputs]
                self.assertTrue(all(0 < len(chunk) <= 1000 for chunk in chunks))
                self.assertEqual("".join(chunks), answer if length <= 3000 else answer[:2999] + "…")

    def test_request_contains_only_question_and_settings(self):
        request = make_request_body("테스트 질문", model="test-model")
        self.assertEqual(request["input"], "테스트 질문")
        self.assertEqual(request["model"], "test-model")
        self.assertFalse(request["store"])
        self.assertNotIn("callbackToken", request)
        self.assertNotIn("BOT_TOKEN", request)

    def test_memory_request_stores_response_and_resends_instructions(self):
        first = make_request_body("첫 질문", remember=True)
        second = make_request_body("다음 질문", remember=True, previous_response_id="resp_first")
        self.assertTrue(first["store"])
        self.assertNotIn("previous_response_id", first)
        self.assertEqual(second["previous_response_id"], "resp_first")
        self.assertEqual(first["instructions"], second["instructions"])

    def test_previous_id_cannot_be_used_with_stateless_mode(self):
        with self.assertRaises(ValueError):
            make_request_body("질문", previous_response_id="resp_first")


if __name__ == "__main__":
    unittest.main()

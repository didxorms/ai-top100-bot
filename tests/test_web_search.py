import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bot.openai_payload import make_request_body, make_skill_response, read_answer
from bot.web_search import render_citations, search_instructions


class WebSearchTests(unittest.TestCase):
    def test_search_can_be_enabled_without_changing_model_or_memory(self):
        body = make_request_body("오늘 뉴스", model="test-model", remember=True,
                                 previous_response_id="resp_before", web_search=True)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["previous_response_id"], "resp_before")
        self.assertEqual(body["tools"], [{"type": "web_search", "search_context_size": "low"}])
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["max_tool_calls"], 3)

    def test_search_off_does_not_send_hosted_tools(self):
        body = make_request_body("오늘 뉴스", web_search=False)
        self.assertNotIn("tools", body)
        self.assertNotIn("max_tool_calls", body)

    def test_relative_dates_use_korean_time_even_when_server_uses_utc(self):
        instant = datetime(2026, 10, 28, 15, 30, tzinfo=timezone.utc)
        with patch("bot.web_search.datetime") as clock:
            clock.now.side_effect = lambda tz: instant.astimezone(tz)
            instructions = search_instructions(True)
        self.assertIn("2026-10-29 00:30 KST", instructions)

    def test_hosted_search_outputs_and_inline_citations_become_clickable_urls(self):
        text = "오늘 소식입니다. ([공식](https://example.com/news))"
        start = text.index("([")
        result = {"status": "completed", "output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": [{
                "type": "url_citation", "start_index": start, "end_index": len(text),
                "url": "https://example.com/news", "title": "공식 발표",
            }]}]},
        ]}
        answer = read_answer(result)
        self.assertIn("오늘 소식입니다.", answer)
        self.assertIn("출처: 공식 발표\nhttps://example.com/news", answer)
        self.assertNotIn("](", answer)

    def test_fact_text_is_preserved_when_annotation_covers_the_claim(self):
        text = "발표일은 10월 29일입니다."
        answer = render_citations(text, [{"type": "url_citation", "start_index": 0,
            "end_index": len(text), "url": "https://example.com", "title": "자료"}])
        self.assertIn(text, answer)
        self.assertIn("https://example.com", answer)

    def test_shared_citation_marker_preserves_multiple_sources(self):
        text = "기사 요약. citeturn0search0turn0search1"
        start = text.index("")
        annotations = [{"type": "url_citation", "start_index": start, "end_index": len(text),
                        "url": url, "title": "기사"}
                       for url in ("https://example.com/one", "https://example.com/two")]
        answer = render_citations(text, annotations)
        self.assertNotIn("", answer)
        for annotation in annotations:
            self.assertIn(annotation["url"], answer)

    def test_invalid_offsets_do_not_remove_the_answer(self):
        answer = render_citations("요약", [{"type": "url_citation", "start_index": -2,
            "end_index": 100, "url": "https://example.com", "title": "자료"}])
        self.assertTrue(answer.startswith("요약"))
        self.assertIn("https://example.com", answer)

    def test_invalid_source_schemes_and_embedded_credentials_are_rejected(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:password@example.com",
                    "https://example.com\nforged", "https://["):
            with self.subTest(url=url), self.assertRaises(ValueError):
                render_citations("요약", [{"type": "url_citation", "url": url}])

    def test_source_link_is_not_split_between_kakao_bubbles(self):
        url = "https://example.com/" + "a" * 200
        text = "가" * 900 + "\n" + url + "\n설명"
        chunks = [output["simpleText"]["text"] for output in make_skill_response(text)["template"]["outputs"]]
        self.assertEqual("".join(chunks), text)
        self.assertTrue(any(url in chunk for chunk in chunks))
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))

    def test_unrenderable_source_is_not_sent_as_a_broken_link(self):
        for text in ("본문\nhttps://example.com/" + "a" * 1100,
                     "가" * 2990 + "\nhttps://example.com/long-link"):
            with self.subTest(length=len(text)):
                outputs = make_skill_response(text)["template"]["outputs"]
                self.assertEqual(len(outputs), 1)
                self.assertIn("질문 범위", outputs[0]["simpleText"]["text"])

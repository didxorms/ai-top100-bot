import unittest

import httpx

from bot.contest_media import transcribe_audio, media_text


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_download_and_transcription_use_separate_headers(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host == "t1.kakaocdn.net":
                self.assertNotIn("Authorization", request.headers)
                return httpx.Response(200, content=b"synthetic audio")
            self.assertEqual(request.url.path, "/v1/audio/transcriptions")
            self.assertEqual(request.headers["Authorization"], "Bearer synthetic")
            return httpx.Response(200, json={"text": "받은 상품 환불이 가능한가요?"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            text=await transcribe_audio(client,"audio: https://t1.kakaocdn.net/aitop100/test.m4a",{"Authorization":"Bearer synthetic"})
        self.assertIn("받은 상품 환불",text)
        self.assertEqual(len(requests),2)

    async def test_arbitrary_audio_url_is_not_downloaded(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: self.fail("must not fetch"))) as client:
            text=await transcribe_audio(client,"https://example.com/private.m4a",{})
        self.assertEqual(text,"https://example.com/private.m4a")

    async def test_reported_phishing_link_is_not_opened_even_on_allowed_cdn(self):
        text="사칭 제보 URL: https://t1.kakaocdn.net/aitop100/report.m4a"
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: self.fail("must not fetch reported URL"))) as client:
            result=await transcribe_audio(client,text,{})
        self.assertEqual(result,text)

    def test_phishing_screenshot_is_distinct_from_reported_link(self):
        text="피싱 제보 URL: https://t1.kakaocdn.net/aitop100/report.png\nimage: https://t1.kakaocdn.net/aitop100/screenshot.png"
        filtered=media_text(text)
        self.assertNotIn("report.png",filtered)
        self.assertIn("screenshot.png",filtered)

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from bot.contest import ContestRuntime, calculate_partial, extract_case_id, run_contest_agent
from bot.openai_payload import make_skill_response


def bundle():
    spec = json.loads((Path(__file__).parents[1] / "TC/actions.json").read_text(encoding="utf-8"))
    return {"api": spec, "documents": {"refund": {"title": "환불", "text": "하자는 원결제로 환불합니다."}},
            "calendar": [{"date": f"2037-06-{day:02}", "business_status": "휴무" if day in [27,28] else "영업",
                          "shipping_status": "출고일" if day in [26,29] else "—"} for day in range(25,31)]}


@unittest.skipUnless((Path(__file__).parents[1] / "TC/actions.json").exists(), "Private competition API fixture required")
class ContestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.db"
        self.runtime = ContestRuntime(self.path, bundle(), token="synthetic-token")
        for scope in ["session:L-11", "session:L-05", "s:L-11"]:
            self.runtime.context(scope, lambda value: value.update(provided=["100100003","missing","1"],refund_requested=True))

    def test_all_40_operations_exclude_reset_and_schema_is_exact(self):
        self.assertEqual(len(self.runtime.operations), 40)
        self.assertNotIn("resetMyActionData", [t["name"] for t in self.runtime.tools])
        self.assertEqual(set(self.runtime.operations["API_35"]["tool"]["parameters"]["properties"]), {"lane","reason"})

    def test_case_header_persists_across_restart_and_isolates_sessions(self):
        self.assertEqual(extract_case_id("case metadata\n- id: L-11\n질문"), "L-11")
        self.assertIsNone(extract_case_id("주문 100100003"))
        self.assertEqual(self.runtime.case_for("one", "TC ID: L-11"), "L-11")
        fresh = ContestRuntime(self.path, bundle(), token="synthetic")
        self.assertEqual(fresh.case_for("one", "후속 질문"), "L-11")
        self.assertIsNone(fresh.case_for("two", "후속 질문"))
        self.assertEqual(fresh.case_for("one", "id: R-05-a"), "R-05-a")

    async def test_fixed_host_schema_validation_and_tc_header(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"order_no":"100100003"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await self.runtime.execute(client,"API_07",'{"order_no":"100100003"}',"session:L-11",{})
            invalid = await self.runtime.execute(client,"API_07",'{"order_no":"1","url":"https://evil.example"}',"session:L-11",{})
            missing = await self.runtime.execute(client,"API_07",'{"order_no":"1"}',None,{})
        self.assertEqual(result["http_status"],200)
        self.assertEqual(requests[0].url.host,"gyodong-mall-action.aitop100.org")
        self.assertEqual(requests[0].headers["X-Aitop100-Test-Case-Id"],"L-11")
        self.assertEqual(invalid["error"],"invalid_tool_or_arguments")
        self.assertEqual(missing["error"],"test_case_id_required")
        self.assertEqual(len(requests),1)

    async def test_persistent_write_dedup_and_scope_isolation(self):
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json={"refund_id":"RF1","status":"SUBMITTED"})
        args=json.dumps({"order_no":"100100003","amount":34900,"reason":"고객선택 전액환불"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first=await self.runtime.execute(client,"API_29",args,"session:L-11",{})
            restarted=ContestRuntime(self.path,bundle(),token="synthetic")
            second=await restarted.execute(client,"API_29",args,"session:L-11",{})
            await restarted.execute(client,"API_29",args,"session:L-05",{})
        self.assertEqual(first,second)
        self.assertIn("접수",first["notice"])
        self.assertEqual(len(requests),2)

    async def test_404_is_not_empty_search_and_reads_are_cached(self):
        count=0
        def handler(request):
            nonlocal count
            count+=1
            return httpx.Response(404) if request.url.path.endswith("missing") else httpx.Response(200,json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            missing=await self.runtime.execute(client,"API_07",'{"order_no":"missing"}',"s:L-11",{})
            cache={}
            empty=await self.runtime.execute(client,"API_11",'{"order_no":"1"}',"s:L-11",cache)
            await self.runtime.execute(client,"API_11",'{"order_no":"1"}',"s:L-11",cache)
        self.assertEqual(missing["http_status"],404)
        self.assertEqual(empty["data"],[])
        self.assertEqual(count,2)

    async def test_invented_target_is_rejected_without_network(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: self.fail("must not call"))) as client:
            result=await self.runtime.execute(client,"API_11",'{"order_no":"100999999"}',"s:L-11",{})
        self.assertEqual(result["error"],"target_not_confirmed")

    async def test_refund_requires_customer_choice(self):
        self.runtime.prime("new:L-11", "주문번호 100100003의 상태가 이상해요")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: self.fail("must not write"))) as client:
            result=await self.runtime.execute(client,"API_29",'{"order_no":"100100003","amount":1000,"reason":"하자 전액환불"}',"new:L-11",{})
        self.assertEqual(result["error"],"customer_choice_required")

    async def test_other_account_shipping_is_never_returned(self):
        self.runtime.prime("private:L-23", "case metadata\n- id: L-23\n- 인입 계정: 회원 로그인 · 테스트 · 019-****-1234\n주문 100100003 배송주소 알려주세요")
        requested=[]
        def handler(request):
            requested.append(request.url.path)
            if request.url.path.startswith("/api/orders/"):
                return httpx.Response(200,json={"order_no":"100100003","member_id":"OTHER","amount":1000,"items":[],"shipping_address":"private address"})
            if request.url.path.startswith("/api/members/"):
                return httpx.Response(200,json={"member_id":"OTHER","name":"다른사람","phone":"019-0000-9999"})
            self.fail("Must not request shipment data for other account")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result=await self.runtime.execute(client,"API_11",'{"order_no":"100100003"}',"private:L-23",{})
        self.assertFalse(result["ownership_verified"])
        self.assertEqual(len(requested),2)
        self.assertNotIn("private address",json.dumps(result))

    async def test_confirmed_multiple_lot_complaints_get_separate_safety_receipt(self):
        self.runtime.context("safe:L-11",lambda value:value.update(safety_lots={"LOT1":1},investigations={"LOT1":False},lot_orders={"LOT1":"100100003"},entered_date="2037-06-25",issue="곰팡"))
        requested=[]
        def handler(request):
            requested.append(json.loads(request.content))
            return httpx.Response(200,json={"escalation_id":"ESC1","status":"SUBMITTED"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            answer=await self.runtime.finish(client,"safe:L-11",{},"개별 구제 안내")
            await self.runtime.finish(client,"safe:L-11",{},"후속 안내")
        self.assertIn("ESC1",answer)
        self.assertIn("100100003",requested[0]["reason"])
        self.assertEqual(requested[0]["lane"],"LANE2_HANDOFF")
        self.assertEqual(len(requested),1)

    def test_partial_refund_boundaries_and_rounding(self):
        self.assertEqual(calculate_partial(45000,60,14)["amount"],10500)
        self.assertEqual(calculate_partial(45000,60,26)["amount"],22500)
        self.assertEqual(calculate_partial(10000,10,3)["band"],"half_refund_or_full_reship")
        self.assertEqual(calculate_partial(10000,10,7)["amount"],5000)
        self.assertEqual(calculate_partial(10000,10,8)["amount"],10000)
        self.assertEqual(calculate_partial(10000,2500,749)["band"],"half_refund_or_full_reship")
        self.assertEqual(calculate_partial(900,10,1)["amount"],900)
        self.assertIn("error",calculate_partial(1000,2,3))

    def test_business_deadline_skips_weekend_and_excludes_base(self):
        self.assertEqual(self.runtime.deadline("2037-06-26",2,"deadline")["date"],"2037-06-30")
        self.assertEqual(self.runtime.deadline("2037-06-27",1,"deadline")["date"],"2037-06-30")
        self.assertEqual(self.runtime.deadline("2037-06-30",2,"cutoff")["date"],"2037-06-26")
        self.assertEqual(self.runtime.deadline("2037-06-26",0,"shipping")["date"],"2037-06-29")
        self.assertIn("error",self.runtime.deadline("2038-01-01",3,"deadline"))

    def test_competition_reply_one_bubble_500_chars(self):
        response=make_skill_response("가"*1001,max_chars=500)
        self.assertEqual(len(response["template"]["outputs"]),1)
        self.assertEqual(len(response["template"]["outputs"][0]["simpleText"]["text"]),500)
        plain=make_skill_response("**접수** 019-****-1234",max_chars=500)
        self.assertEqual(plain["template"]["outputs"][0]["simpleText"]["text"],"접수 019-****-1234")

    async def test_tool_loop_supports_more_than_four_calls(self):
        count=0
        def handler(request):
            nonlocal count
            count+=1
            if count==1:
                output=[{"type":"function_call","name":"read_policy","arguments":'{"doc_id":"refund","offset":0}',"call_id":f"c{i}"} for i in range(6)]
            else:
                data=json.loads(request.content)
                self.assertEqual(len(data["input"]),6)
                output=[{"type":"message","content":[{"type":"output_text","text":"환불 접수 안내"}]}]
            return httpx.Response(200,json={"id":f"resp_{count}","status":"completed","output":output})
        body={"model":"synthetic","instructions":"test","input":"test","store":True}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            _,answer=await run_contest_agent(client,body,{},self.runtime,"s:L-11")
        self.assertEqual(answer,"환불 접수 안내")

    async def test_receipt_is_returned_if_model_fails_after_write(self):
        count=0
        def handler(request):
            nonlocal count
            if request.url.host == "api.openai.com":
                count+=1
                if count==1:
                    return httpx.Response(200,json={"id":"resp1","output":[{"type":"function_call","name":"API_29","arguments":'{"order_no":"100100003","amount":1000,"reason":"하자 환불"}',"call_id":"call1"}]})
                raise httpx.ReadTimeout("synthetic timeout",request=request)
            return httpx.Response(200,json={"refund_id":"RFD-SYNTHETIC","status":"SUBMITTED"})
        body={"model":"synthetic","instructions":"test","input":"test","store":True}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result,answer=await run_contest_agent(client,body,{},self.runtime,"session:L-11")
        self.assertTrue(result["fallback"])
        self.assertIn("RFD-SYNTHETIC",answer)
        self.assertIn("완료는 아닙니다",answer)


if __name__ == "__main__":
    unittest.main()

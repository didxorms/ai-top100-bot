"""Competition runtime. Private policies and API schemas are supplied at deployment time."""

import asyncio
import base64
from collections import deque
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote
import zlib

import httpx
from jsonschema import Draft202012Validator, ValidationError

from .openai_payload import read_answer

API_ORIGIN = "https://gyodong-mall-action.aitop100.org"
TC_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def load_bundle():
    encoded = os.getenv("CONTEST_BUNDLE_B64", "").strip()
    if not encoded:
        encoded = "".join(os.getenv(f"CONTEST_BUNDLE_{i}", "") for i in range(1, 17))
    if encoded:
        return json.loads(zlib.decompress(base64.b64decode(encoded)))
    path = Path(os.getenv("CONTEST_BUNDLE_PATH", ".local/contest/runtime-bundle.json"))
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    raise ValueError("Competition knowledge bundle is missing")


def tool(name, description, properties):
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}


def build_operations(spec):
    operations = {}
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            if not operation.get("operationId", "").startswith("API-"):
                continue  # Reset and administrative endpoints are never model tools.
            properties = {}
            for parameter in operation.get("parameters", []):
                if parameter["in"] in {"path", "query"}:
                    properties[parameter["name"]] = parameter["schema"]
            body_schema = operation.get("requestBody", {}).get("content", {}).get(
                "application/json", {}).get("schema")
            properties.update((body_schema or {}).get("properties", {}))
            name = operation["operationId"].replace("-", "_")
            operations[name] = {"method": method.upper(), "path": path.split("?", 1)[0],
                                "parameters": operation.get("parameters", []),
                                "body_schema": body_schema,
                                "tool": tool(name, operation["summary"], properties)}
    return operations


def extract_case_id(text):
    # A case ID must be explicitly labelled; never infer one from an order number.
    patterns = [r"(?im)^\s*[-*]?\s*(?:id|tc(?:\s*id)?|test[_ -]?case[_ -]?id|테스트\s*케이스\s*(?:id|아이디))\s*[:：=]\s*[`\"']?([A-Za-z0-9][A-Za-z0-9._-]{0,99})",
                r"(?i)\bTC\s*(?:ID)?\s*[:：=]\s*[`\"']?([A-Za-z0-9][A-Za-z0-9._-]{0,99})"]
    for pattern in patterns:
        found = re.search(pattern, text)
        if found:
            return found.group(1)
    return None

class ContestRuntime:
    def __init__(self, path, bundle=None, token=None):
        self.path = Path(path)
        self.bundle = bundle if bundle is not None else load_bundle()
        self.operations = build_operations(self.bundle["api"])
        self.token = token if token is not None else os.getenv("CONTEST_ACTION_TOKEN", "").strip()
        self.window = deque()
        self.rate_lock = asyncio.Lock()
        self.tools = [op["tool"] for op in self.operations.values()] + [
            tool("read_policy", "규정·보유 자료를 원문/요약으로 읽습니다. doc_id는 제공된 문서목록 값.",
                 {"doc_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}}),
            tool("search_policy", "보유 문서의 문구/상품/날짜를 검색합니다. 웹 검색이 아닙니다.",
                 {"query": {"type": "string"}}),
            tool("partial_refund", "복수개체 하자 산식의 반올림 구간과 환불액 계산. 실제 접수는 별도 API29.",
                 {"paid_amount": {"type": "integer", "minimum": 0},
                  "total_count": {"type": "integer", "minimum": 1},
                  "defect_count": {"type": "integer", "minimum": 1}}),
            tool("business_deadline", "업무 달력으로 영업일 기한/결제일 컷오프 역산/다음 출고일 계산.",
                 {"start_date": {"type": "string"}, "days": {"type": "integer", "minimum": 0, "maximum": 30},
                  "kind": {"type": "string", "enum": ["deadline", "cutoff", "shipping"]}}),
        ]

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS contest_sessions (session TEXT PRIMARY KEY, tc TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS contest_receipts (scope TEXT, request_key TEXT, result TEXT, PRIMARY KEY(scope,request_key))")
        db.execute("CREATE TABLE IF NOT EXISTS contest_context (scope TEXT PRIMARY KEY, value TEXT NOT NULL)")
        try:
            with db:
                yield db
        finally:
            db.close()

    def case_for(self, session, text):
        incoming = extract_case_id(text)
        if not session:
            return incoming
        with self._connect() as db:
            if incoming:
                db.execute("INSERT INTO contest_sessions VALUES (?,?) ON CONFLICT(session) DO UPDATE SET tc=excluded.tc", (session, incoming))
                return incoming
            row = db.execute("SELECT tc FROM contest_sessions WHERE session=?", (session,)).fetchone()
            return row[0] if row else None

    def reset(self, session):
        with self._connect() as db:
            db.execute("DELETE FROM contest_sessions WHERE session=?", (session,))

    def context(self, scope, update=None):
        with self._connect() as db:
            row = db.execute("SELECT value FROM contest_context WHERE scope=?", (scope,)).fetchone()
            value = json.loads(row[0]) if row else {"provided": [], "observed": []}
            if update:
                update(value)
                db.execute("INSERT OR REPLACE INTO contest_context VALUES (?,?)", (scope,json.dumps(value,ensure_ascii=False)))
            return value

    def prime(self, scope, text):
        if not scope:
            return
        def update(value):
            identifiers = re.findall(r"(?<![A-Za-z0-9])(?:[0-9]{9,12}|(?:SHP|PAY|SUB|KD|GM|ESC|MBR|MEM)-[A-Za-z0-9_-]+)(?![A-Za-z0-9])", text)
            value["provided"] = sorted(set(value["provided"] + identifiers))
            issue = next((word for word in ["곰팡", "부패", "재냉동", "해동"] if word in text), None)
            if issue:
                value["issue"] = issue
            entered = re.search(r"상담\s*인입\s*일시\s*[:：]\s*(\d{4}-\d{2}-\d{2})",text)
            if entered:
                value["entered_date"] = entered.group(1)
            explicit_refund = bool(re.search(r"환불(?:로|을|\s)*(?:해|할게|할래|받겠|받을게|선택|부탁|원합|원해|진행|신청)|환불\s*주세요", text))
            explicit_reship = bool(re.search(r"재배송(?:으로|을|\s)*(?:해|할게|할래|받겠|받을게|선택|부탁|원합|원해|진행|신청)|재배송\s*주세요", text))
            if explicit_refund:
                value["refund_requested"] = True
            if explicit_reship:
                value["reship_requested"] = True
            if not value.get("account_name"):
                account = re.search(r"인입\s*계정\s*[:：]\s*회원\s*로그인\s*[·ㆍ|]\s*([^·ㆍ|\n]+)[·ㆍ|]\s*([^\n]+)", text)
                if account:
                    value["account_name"] = account.group(1).strip()
                    digits = re.findall(r"\d{4}", account.group(2))
                    value["account_last4"] = digits[-1] if digits else None
        self.context(scope, update)

    def observe(self, scope, name, data):
        if name in {"API_05", "API_06", "API_09", "API_14"}:
            return  # Other customers in evidence/search results do not grant access.
        identifiers = []
        def walk(value):
            if isinstance(value, dict):
                for key,item in value.items():
                    if key in {"order_no","member_id","lot_id","shipment_id","approval_no","subscription_id","escalation_id"} and isinstance(item,str):
                        identifiers.append(item)
                    elif key == "payment_refs" and isinstance(item,list):
                        identifiers.extend(x for x in item if isinstance(x,str))
                    else:
                        walk(item)
            elif isinstance(value,list):
                for item in value:
                    walk(item)
        walk(data)
        def update(value):
            value["observed"] = sorted(set(value["observed"]+identifiers))
            if name == "API_07" and isinstance(data,dict):
                value.setdefault("orders", {})[data["order_no"]] = {
                    "amount": data.get("amount"), "member_id": data.get("member_id"),
                    "single_item": len(data.get("items",[])) == 1 and data["items"][0].get("qty") == 1}
            if name in {"API_10", "API_11"}:
                for row in data if isinstance(data,list) else [data]:
                    if isinstance(row,dict) and row.get("shipment_id"):
                        value.setdefault("shipment_orders", {})[row["shipment_id"]] = row.get("order_no")
                        for item in row.get("items", []):
                            if item.get("lot_id"):
                                value.setdefault("lot_orders", {})[item["lot_id"]] = row.get("order_no")
            if name in {"API_12", "API_13"}:
                rows = data if isinstance(data,list) else [data]
                for row in rows:
                    if not isinstance(row,dict):
                        continue
                    phone = re.sub(r"\D", "", str(row.get("phone", "")))
                    if value.get("account_name") == row.get("name") and value.get("account_last4") and phone.endswith(value["account_last4"]):
                        value["verified_members"] = sorted(set(value.get("verified_members", [])+[row["member_id"]]))
        self.context(scope, update)

    def instructions(self, text, tc):
        docs = self.bundle["documents"]
        index = "\n".join(f"{key}: {doc['title']}" for key, doc in docs.items())
        selected = []
        routes = {
            "product-page-policy": ["당도", "시어", "셔요", "감귤", "브릭스", "보관", "수박"],
            "partial-refund-policy": ["부분환불", "몇 개", "몇개", "곰팡이", "상했", "상한", "박스", "하자"],
            "payment-reconciliation-policy": ["결제", "승인", "주문이 없", "주문이 안", "중복"],
            "privacy-policy": ["개인정보", "주소", "전화", "연락처", "누구", "정보", "아버지", "친구", "가족"],
            "third-party-policy": ["기사", "농가", "직원", "개인", "연락처", "가족", "대리", "중계"],
            "packing-policy": ["동봉", "구판", "안내문", "포장", "완충", "3일"],
            "subscription-policy": ["정기배송", "구독", "스킵", "해지", "컷오프"],
            "cold-chain-handover-policy": ["냉동", "옥돔", "해동", "온도", "콜드체인"],
            "promo-master-policy": ["쿠폰", "코드", "공구", "할인", "보상", "SUBAK", "CARE"],
            "membership-terms-policy": ["조합원", "회원", "탈퇴", "누적", "추천", "적립", "단골"],
            "experience-pamphlet": ["체험", "견학", "단체", "명단", "예약"],
            "live-archive-policy": ["라이브", "홍보걸", "배너", "게시물", "무료배송"],
            "season-weather-policy": ["결항", "태풍", "기상", "제철", "작황", "예약출고"],
            "seafood-weight-policy": ["중량", "무게", "자연감량", "드립"],
            "policy-revisions-policy": ["차액", "가격", "3일", "7일", "예전", "과거"],
            "safe-policy": ["죽", "자해", "자살", "살기", "위협"],
            "gift-policy": ["선물세트", "포장", "카드 문구", "각인"],
            "wholesale-policy": ["도매", "사업자", "대량", "재판매"],
            "festival-policy": ["축제", "매대", "직거래", "오프라인"],
            "receipts-policy": ["영수증", "세금계산서", "증빙"],
        }
        scored = sorted(((sum(word.lower() in text.lower() for word in words), key) for key, words in routes.items()), reverse=True)
        for score, key in scored:
            if score and key in docs:
                selected.append(docs[key]["text"])
            if len(selected) == 4:
                break
        return self.bundle.get("instructions", "") + self.bundle.get("public_context", "") + "\n현재 TC: " + (tc or "미제공. 액션 전에 TC ID를 요청하세요.") + "\n관련 확보 자료:\n" + "\n\n".join(selected) + "\n문서목록:\n" + index

    async def execute(self, client, name, arguments, scope, cache):
        try:
            args = json.loads(arguments)
            definition = next(t for t in self.tools if t["name"] == name)
            Draft202012Validator(definition["parameters"]).validate(args)
        except (ValueError, TypeError, StopIteration, ValidationError):
            return {"error": "invalid_tool_or_arguments"}
        if name == "read_policy":
            doc = self.bundle["documents"].get(args["doc_id"])
            if not doc:
                return {"error": "unknown_document", "documents": list(self.bundle["documents"])}
            content = doc["text"]
            offset = args["offset"]
            return {"title": doc["title"], "source": doc.get("source", ""), "text": content[offset:offset+16000],
                    "next_offset": offset+16000 if len(content) > offset+16000 else None}
        if name == "search_policy":
            terms = [x for x in re.split(r"\s+", args["query"].lower()) if len(x) > 1]
            hits = []
            for key, doc in self.bundle["documents"].items():
                score = sum(doc["text"].lower().count(t) for t in terms)
                if score:
                    positions = [doc["text"].lower().find(t) for t in terms if t in doc["text"].lower()]
                    pos = min(positions)
                    hits.append((score, {"doc_id": key, "title": doc["title"], "excerpt": doc["text"][max(0,pos-250):pos+1500]}))
            return {"results": [hit for _, hit in sorted(hits, key=lambda x: x[0], reverse=True)[:6]]}
        if name == "partial_refund":
            return calculate_partial(**args)
        if name == "business_deadline":
            return self.deadline(**args)
        if not scope or not TC_PATTERN.fullmatch(scope.split(":")[-1]):
            return {"error": "test_case_id_required", "message": "대화에 명시된 TC ID를 확인해 주세요."}
        if not self.token:
            return {"error": "action_token_not_configured"}
        operation = self.operations[name]
        context = self.context(scope)
        # Identifiers may come only from the actual conversation or earlier scoped API results.
        # Examples in documents and IDs invented by the model never grant access.
        permitted = set(context["provided"] + context["observed"])
        for identifier in ("order_no", "member_id", "lot_id", "shipment_id", "approval_no", "subscription_id", "escalation_id"):
            if identifier in args and args[identifier] not in permitted:
                return {"error": "target_not_confirmed", "field": identifier,
                        "message": "대화에 고객이 제시하거나 해당 사건의 조회로 확인한 식별자만 사용하세요. 추측/자료 예시 ID 사용 금지. 대상 번호를 먼저 물어보세요."}
        if name == "API_13" and context.get("account_name") and args["name"] != context["account_name"]:
            return {"error": "other_customer_lookup_forbidden"}
        if name in {"API_05", "API_08", "API_15", "API_16"} and args["member_id"] not in context.get("verified_members", []):
            return {"error": "member_ownership_check_required", "message": "먼저 API12로 인입 계정과의 귀속을 확인하세요."}
        if name == "API_14" and args["referred_by"] not in context.get("verified_members", []):
            return {"error": "member_ownership_check_required"}
        if name == "API_11" and context.get("account_name"):
            order = context.get("orders", {}).get(args["order_no"])
            if not order:
                await self.execute(client, "API_07", json.dumps({"order_no": args["order_no"]}), scope, cache)
                context = self.context(scope)
                order = context.get("orders", {}).get(args["order_no"])
            if order and order.get("member_id") not in context.get("verified_members", []):
                return {"error": "order_ownership_mismatch", "ownership_verified": False,
                        "message": "본인 계정 주문 아님. 배송상태·주소 일부도 안내 금지. 개인정보 요청은 API35 LANE2_HANDOFF SECURITY로 접수하세요."}
        if name == "API_29":
            if not context.get("refund_requested"):
                return {"error": "customer_choice_required", "message": "고객이 환불을 선택하지 않았습니다. 가능한 재배송/환불을 먼저 안내하고 선택을 받으세요."}
            order = context.get("orders", {}).get(args["order_no"])
            if order and (args["amount"] > order["amount"] or (order["single_item"] and "전액" in args["reason"] and args["amount"] != order["amount"])):
                return {"error": "refund_amount_review_required", "order_paid_amount": order["amount"],
                        "message": "단일품목1개 주문의 전액환불은 주문 실결제 총액입니다. 단가와 배송비 등 차이를 확인하세요. 일부 품목/부분하자는 별도 산식."}
        if name == "API_28" and not context.get("reship_requested"):
            return {"error": "customer_choice_required", "message": "재배송을 원하는 고객의 명시적 선택이 필요합니다."}
        if operation["method"] == "POST" and "member_id" in args and args["member_id"] not in context.get("verified_members", []):
            return {"error": "member_ownership_check_required", "message": "API12/13으로 인입 계정의 이름·전화 끝자리와 회원 귀속을 대조해야 합니다."}
        target_order = args.get("order_no")
        if "shipment_id" in args:
            target_order = context.get("shipment_orders", {}).get(args["shipment_id"])
        if operation["method"] == "POST" and target_order:
            order = context.get("orders", {}).get(target_order)
            if not order and context.get("account_name"):
                return {"error": "order_lookup_required", "order_no": target_order,
                        "message": "API07로 해당 주문과 회원 귀속을 확인한 후 처리하세요."}
            if order and context.get("account_name") and order.get("member_id") not in context.get("verified_members", []):
                return {"error": "order_ownership_check_required", "member_id": order.get("member_id"),
                        "message": "API12로 해당 주문의 회원이 인입 계정에 귀속되는지 확인하세요. 다른 고객 주문은 처리 금지."}
        path = operation["path"]
        query = {}
        for param in operation["parameters"]:
            key = param["name"]
            if param["in"] == "path":
                path = path.replace("{" + key + "}", quote(args[key], safe=""))
            elif param["in"] == "query":
                query[key] = args[key]
        body_schema = operation["body_schema"]
        payload = {key: args[key] for key in (body_schema or {}).get("properties", {})}
        if body_schema is not None:
            Draft202012Validator(body_schema).validate(payload)
        if name == "API_35" and payload["lane"] not in {"LANE2_HANDOFF", "LANE3_CLAIM", "LANE1_BOT"}:
            return {"error": "invalid_escalation_lane"}
        if any(type(args.get(k)) is int and args[k] <= 0 for k in ("amount", "qty", "charge_amount")):
            return {"error": "amount_and_quantity_must_be_positive"}
        key = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
        if operation["method"] == "GET" and key in cache:
            return cache[key]
        # Exact repeated writes are served from the persistent receipt journal, including after restart.
        if operation["method"] == "POST":
            with self._connect() as db:
                row = db.execute("SELECT result FROM contest_receipts WHERE scope=? AND request_key=?", (scope,key)).fetchone()
            if row:
                return json.loads(row[0])
        headers = {"Authorization": "Bearer " + self.token,
                   "X-Aitop100-Test-Case-Id": scope.split(":")[-1]}
        result = None
        for attempt in range(2):
            async with self.rate_lock:
                now = time.monotonic()
                while self.window and now-self.window[0] >= 60:
                    self.window.popleft()
                if len(self.window) >= 95:
                    return {"error": "rate_limit", "retry_after": 60-(now-self.window[0])}
                self.window.append(now)
            try:
                response = await client.request(operation["method"], API_ORIGIN+path,
                                                params=query, json=payload if body_schema is not None else None,
                                                headers=headers, timeout=8)
            except httpx.HTTPError:
                return {"error": "transport_error", "state": "unconfirmed", "message": "성공 여부 미확인. 완료라고 안내하지 마세요."}
            print("대회 액션:", name, response.status_code, flush=True)
            try:
                data = response.json() if response.content else None
            except ValueError:
                data = None
            if response.status_code == 429:
                if isinstance(data, dict) and "누적" in str(data.get("message", "")):
                    return {"error": "cumulative_limit", "http_status": 429}
                try:
                    delay = max(0.1, float(response.headers.get("Retry-After", "1")))
                except ValueError:
                    delay = 1
                if attempt == 0 and delay <= 8:
                    await asyncio.sleep(delay)
                    continue
                return {"error": "rate_limit", "retry_after": delay}
            if response.status_code in {500,503} and attempt == 0:
                await asyncio.sleep(0.5)
                continue
            if response.status_code == 404 and name == "API_04":
                self.context(scope,lambda value:value.setdefault("investigations",{}).update({args["lot_id"]:False}))
                return {"http_status": 404, "data": None, "finding": "해당 로트 조사 기록 없음. 로트 자체 존재와 신고 티켓은 별도 조회."}
            if response.is_error:
                return {"error": "action_api_error", "http_status": response.status_code, "detail": data,
                        "message": "404는 경로/키 확인 후 미확인·미존재를 구별. 오류를 정상 결과로 해석하지 마세요."}
            result = {"http_status": response.status_code, "data": data}
            break
        if result is None:
            return {"error": "action_unavailable"}
        if operation["method"] == "POST":
            result["notice"] = "요청 접수 기록입니다. 실제 상태 변경/승인/지급 완료가 아닙니다."
            with self._connect() as db:
                db.execute("INSERT OR REPLACE INTO contest_receipts VALUES (?,?,?)", (scope,key,json.dumps(result, ensure_ascii=False)))
            self.context(scope,lambda value:value.setdefault("last_receipts",{}).update({name:{"summary":operation["tool"]["description"],"data":data}}))
            if name == "API_35":
                covered = [lot for lot in context.get("safety_lots", {}) if lot in args["reason"]]
                if covered:
                    self.context(scope, lambda value: value.update(safety_notified=sorted(set(value.get("safety_notified",[])+covered))))
            cache.clear()
        else:
            self.observe(scope, name, data)
            if name == "API_04":
                self.context(scope,lambda value:value.setdefault("investigations",{}).update({args["lot_id"]:True}))
            if name == "API_06" and isinstance(data,list) and context.get("issue"):
                matching = sum(context["issue"] in str(row.get("content", "")) for row in data if isinstance(row,dict))
                if matching:
                    self.context(scope, lambda value: value.setdefault("safety_lots", {}).update({args["lot_id"]: matching}))
            if name == "API_07" and isinstance(data,dict) and data.get("member_id") and context.get("account_name"):
                await self.execute(client, "API_12", json.dumps({"member_id": data["member_id"]}), scope, cache)
                verified = self.context(scope).get("verified_members", [])
                result["ownership_verified"] = data["member_id"] in verified
                result["privacy_notice"] = "주소·수령인 가림은 개인정보 최소화이며 정보 불일치가 아닙니다. 서버의 계정 귀속 대조 결과를 사용하세요."
                if not result["ownership_verified"]:
                    result["data"] = {"order_no": data["order_no"]}
                    result["notice"] = "본인 주문 아님. 타계정 배송·주소 안내 금지. API35 LANE2_HANDOFF로 담당 확인 접수."
            if name in {"API_12", "API_13"} and context.get("account_name"):
                verified = self.context(scope).get("verified_members", [])
                if isinstance(result["data"],dict) and result["data"].get("member_id") not in verified:
                    result["data"] = {"ownership_verified": False}
                elif isinstance(result["data"],list):
                    result["data"] = [row for row in result["data"] if row.get("member_id") in verified]
            if name in {"API_07", "API_12", "API_13"}:
                def redact(value):
                    if isinstance(value,dict):
                        return {key: ("[비공개: 고객 선제시 후 대조]" if key in {"address","shipping_address","recipient_name"}
                                      else ("****-"+str(item)[-4:] if key == "phone" and item else redact(item)))
                                for key,item in value.items()}
                    if isinstance(value,list):
                        return [redact(item) for item in value]
                    return value
                result["data"] = redact(result["data"])
            cache[key] = result
        return result

    async def finish(self, client, scope, cache, answer):
        if not scope:
            return answer
        context = self.context(scope)
        receipts = []
        for lot, count in context.get("safety_lots", {}).items():
            if lot in context.get("safety_notified", []):
                continue
            if lot not in context.get("investigations", {}):
                await self.execute(client,"API_04",json.dumps({"lot_id":lot}),scope,cache)
                context = self.context(scope)
            investigation = "조사기록 없음" if context.get("investigations", {}).get(lot) is False else "조사 상태 추가 확인 필요"
            # An independent safety review is required even when the individual remedy is settled.
            reason = (f"대상 주문 {context.get('lot_orders',{}).get(lot,'확인 중')}, 로트 {lot}; 고객 {context.get('issue','하자')} 신고와 동일 로트 기존 유사 신고 {count}건 확인. "
                      f"{investigation}. 안전 확인 및 판매자·공급 확인 요청. 담당 SUPPLY, 우선순위 긴급. 개별 구제와 별도 이관, 운영자 경유.")
            result = await self.execute(client,"API_35",json.dumps({"lane":"LANE2_HANDOFF","reason":reason},ensure_ascii=False),scope,cache)
            if result.get("http_status") == 200 and isinstance(result.get("data"),dict):
                data = result["data"]
                receipt = data.get("escalation_id")
                if receipt:
                    self.context(scope,lambda value: value.update(safety_notified=sorted(set(value.get("safety_notified",[])+[lot]))))
                    deadline = self.deadline(context.get("entered_date", "2037-06-30"),3,"deadline")
                    self.context(scope,lambda value: value.setdefault("safety_receipts",{}).update({lot:{"escalation_id":receipt,"deadline":deadline.get("date")}}))
                    receipts.append(f"같은 로트 안전 확인도 별도 접수했습니다({receipt}). 회신 예정: {deadline.get('date','확인 중')} 24:00.")
        if receipts:
            suffix = "\n" + "\n".join(receipts)
            answer = answer[:max(0,500-len(suffix))] + suffix
        return answer

    def deadline(self, start_date, days, kind):
        try:
            start = date.fromisoformat(start_date)
        except ValueError:
            return {"error": "invalid_date"}
        dates = {row["date"]: row for row in self.bundle["calendar"]}
        def business(d):
            row = dates.get(d.isoformat())
            if row is None:
                raise ValueError("calendar_out_of_range")
            return row["business_status"] == "영업"
        try:
            current = start
            if kind == "shipping":
                for _ in range(32):
                    current += timedelta(days=1)
                    if business(current) and dates[current.isoformat()]["shipping_status"] == "출고일":
                        return {"date": current.isoformat(), "notice": "정기 출고일이며 개별 출고 확정 아님"}
                return {"error": "shipping_date_not_found"}
            if kind == "deadline":
                while not business(current):
                    current += timedelta(days=1)
            remaining = days
            while remaining:
                current += timedelta(days=-1 if kind == "cutoff" else 1)
                if business(current):
                    remaining -= 1
            return {"date": current.isoformat(), "time": "24:00", "days": days, "kind": kind}
        except ValueError:
            return {"error": "calendar_out_of_range"}


def calculate_partial(paid_amount, total_count, defect_count):
    if total_count < 1 or not 1 <= defect_count <= total_count or paid_amount < 0:
        return {"error": "invalid_count"}
    ratio = Decimal(defect_count)/Decimal(total_count)
    percent = (ratio*100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if percent < 30:
        value = Decimal(paid_amount)*ratio
        band = "proportional"
    elif percent <= 70:
        value = Decimal(paid_amount)/2
        band = "half_refund_or_full_reship"
    else:
        value = Decimal(paid_amount)
        band = "full_refund_or_full_reship"
    amount = min(paid_amount, max(1000, int((value/10).quantize(Decimal("1"), rounding=ROUND_CEILING))*10))
    return {"ratio_percent": str(percent), "amount": amount, "band": band,
            "notice": "복수개체 하자 산식. 단품·무게미달·보관귀책은 별도 규정. 실결제액 상한. 실제 접수 아님."}


async def run_contest_agent(client, body, headers, runtime, scope):
    body = {**body, "tools": runtime.tools, "parallel_tool_calls": True,
            "max_output_tokens": 1800}
    body.pop("max_tool_calls", None)
    cache = {}
    if body["model"].startswith(("gpt-5", "gpt-6")):
        body["reasoning"] = {"effort": "low"}
    history = body["input"].copy() if isinstance(body["input"], list) else [{"role": "user", "content": body["input"]}]
    used = 0
    started = time.monotonic()
    receipts = []
    def receipt_answer():
        return "\n".join(receipts) + "\n요청 접수 단계이며 실제 승인·지급·출고 완료는 아닙니다."
    async def finalize(answer):
        try:
            return await asyncio.wait_for(runtime.finish(client,scope,cache,answer),timeout=4)
        except (TimeoutError,httpx.HTTPError):
            return answer
    for round_number in range(13):
        if round_number == 12 or used >= 20:
            body["tool_choice"] = "none"
        remaining = 42-(time.monotonic()-started)
        if remaining <= 1:
            if receipts:
                answer = await finalize(receipt_answer())
                return {"fallback": True}, answer
            raise ValueError("Competition response deadline reached")
        try:
            response = await client.post("https://api.openai.com/v1/responses", headers=headers, json=body, timeout=min(18,remaining))
            response.raise_for_status()
        except httpx.HTTPError:
            if receipts:
                answer = await finalize(receipt_answer())
                return {"fallback": True}, answer
            raise
        result = response.json()
        calls = [x for x in result.get("output", []) if x.get("type") == "function_call"]
        if not calls:
            answer = await finalize(read_answer(result))
            return result, answer
        if used + len(calls) > 20 or body.get("tool_choice") == "none":
            raise ValueError("Competition tool budget exceeded")
        outputs = []
        # Execute in order so reads precede dependent writes and repeated writes are journaled.
        for call in calls:
            if not call.get("call_id"):
                raise ValueError("Missing tool call ID")
            print("대회 도구:", call.get("name"), flush=True)
            tool_result = await runtime.execute(client, call.get("name"), call.get("arguments"), scope, cache)
            operation = runtime.operations.get(call.get("name"), {})
            if operation.get("method") == "POST" and tool_result.get("http_status") == 200:
                data = tool_result.get("data")
                if isinstance(data,dict):
                    ids = [str(value) for key,value in data.items() if key.endswith("_id") or key == "id"]
                    receipts.append(operation["tool"]["description"] + " 요청 접수" + (": " + ", ".join(ids) if ids else ""))
            outputs.append({"type": "function_call_output", "call_id": call["call_id"],
                            "output": json.dumps(tool_result, ensure_ascii=False)})
        used += len(calls)
        if body.get("store"):
            if not result.get("id"):
                raise ValueError("Missing response ID")
            body = {**body, "previous_response_id": result["id"], "input": outputs}
        else:
            history.extend(result["output"])
            history.extend(outputs)
            body = {**body, "input": history.copy()}
    raise ValueError("Competition loop did not finish")

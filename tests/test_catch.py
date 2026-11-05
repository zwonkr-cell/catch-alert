# -*- coding: utf-8 -*-
"""
캐치 봇 오프라인 테스트 — 로컬 가짜 캐치 API 로 수집·페이징·전송 흐름을 검증합니다.
실행: python -m unittest discover -s tests -v   (리포 루트에서, 인터넷 불필요)
"""
import os
import sys
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = "127.0.0.1,localhost"
os.environ.pop("NETKIT_FORCE_PROXY", None)

import netkit  # noqa: E402
import catch_alert as ca  # noqa: E402


def make_item(rid):
    return {"RecruitID": rid, "CompName": "회사%d" % rid, "RecruitTitle": "공고 <%d>" % rid,
            "WorkArea": "서울", "Depth": "대기업", "PopularCategory": "마케팅",
            "CareerGubunCode": "신입", "GubunCode": "정규직", "RecruitCategory": "공채",
            "ApplyEndDatetime": "2026-11-20T14:59:59Z", "Dday": 15, "ApplyEndCode": ""}


class FakeCatch(BaseHTTPRequestHandler):
    items = []
    fail_pages = set()
    mode = "ok"   # ok | nodata(구조 변경) | html

    def log_message(self, *a):
        pass

    def do_GET(self):
        q = parse_qs(urlsplit(self.path).query)
        page = int(q.get("curpage", ["1"])[0])
        size = int(q.get("pageSize", ["30"])[0])
        if page in FakeCatch.fail_pages:
            return self._send(500, b"oops")
        if FakeCatch.mode == "html":
            return self._send(200, "<html>점검중</html>".encode("utf-8"))
        chunk = FakeCatch.items[(page - 1) * size: page * size]
        body = {"recruitData": chunk, "intTotalRecordCount": len(FakeCatch.items)}
        if FakeCatch.mode == "nodata":
            body = {"list": chunk, "total": len(FakeCatch.items)}
        # 실제 API 처럼 BOM 이 붙어 와도 처리되는지
        return self._send(200, b"\xef\xbb\xbf" + json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def _send(self, code, b):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class CatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCatch)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = "http://127.0.0.1:%d/api/v1.0/recruit/information/getRecruitList?Keyword=&Sort=0&curpage=1&pageSize=30" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeCatch.items = [make_item(500000 + i) for i in range(102)]
        FakeCatch.fail_pages = set()
        FakeCatch.mode = "ok"
        self._orig_fetch = netkit.fetch_proxy_candidates
        netkit.fetch_proxy_candidates = lambda sources=None, **k: ([], "test")   # 외부 프록시 목록 안 받음
        self.tmp = tempfile.mkdtemp()
        self._orig_state = ca.STATE_PATH
        ca.STATE_PATH = os.path.join(self.tmp, "seen_ids.json")
        self.sent = []
        self._orig_notify = ca.notify_recipients
        ca.notify_recipients = lambda token, chat_ids, text, cfg: self.sent.append(text) or True
        self._orig_sheet = ca.log_to_sheet
        self.sheet = []
        ca.log_to_sheet = lambda p: self.sheet.append(p)
        self._orig_sleep = ca.time.sleep
        self.cfg = {"bot_token": "x", "chat_ids": ["1"], "api_url": self.api, "request_timeout": 10,
                    "disable_web_page_preview": True, "weekday_full": True,
                    "send_summary_on_first_run": True, "max_notify_per_run": 20}

    def tearDown(self):
        netkit.fetch_proxy_candidates = self._orig_fetch
        ca.STATE_PATH = self._orig_state
        ca.notify_recipients = self._orig_notify
        ca.log_to_sheet = self._orig_sheet

    def net(self, state=None):
        n = ca.make_net(self.cfg, state if state is not None else {})
        n.head_start = 0.2
        return n

    def test_validate(self):
        self.assertEqual(ca._validate_catch(403, "")[0], "blocked")
        self.assertEqual(ca._validate_catch(200, "<html>")[0], "bad")
        self.assertEqual(ca._validate_catch(200, '{"list": []}')[0], "empty")
        self.assertEqual(ca._validate_catch(200, '{"recruitData": "x"}')[0], "empty")
        self.assertEqual(ca._validate_catch(200, '{"recruitData": null}')[0], "ok")
        self.assertEqual(ca._validate_catch(200, '{"recruitData": [], "intTotalRecordCount": 0}')[0], "ok")

    def test_fetch_all_pages(self):
        items, total, complete = ca.fetch_all(self.cfg, self.net())
        self.assertEqual((len(items), total, complete), (102, 102, True))

    def test_fetch_partial_when_page2_fails(self):
        FakeCatch.fail_pages = {2}
        items, total, complete = ca.fetch_all(self.cfg, self.net())
        self.assertEqual((len(items), complete), (100, False))

    def test_structure_change_message(self):
        FakeCatch.mode = "nodata"
        with self.assertRaises(RuntimeError) as cm:
            ca.fetch_all(self.cfg, self.net())
        self.assertIn("구조 변경", str(cm.exception))
        self.assertEqual(ca.classify_error(str(cm.exception))["key"], "structure")

    def test_network_message_is_noncritical(self):
        FakeCatch.fail_pages = {1}
        with self.assertRaises(RuntimeError) as cm:
            ca.fetch_all(self.cfg, self.net())
        self.assertEqual(ca.classify_error("RuntimeError: " + str(cm.exception))["key"], "proxy")

    def test_run_seed_then_new(self):
        state = {}
        ca.run(self.cfg, state)                       # 첫 실행 = 무음 시딩 + 연결 안내 1건
        self.assertEqual(len(self.sent), 1)
        self.assertIn("정상 연결", self.sent[0])
        with open(ca.STATE_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["count"], 102)
        # 새 공고 3건 → 오래된 것부터 3건 전송, 시트 기록은 전송 후 일괄
        FakeCatch.items = [make_item(600002), make_item(600001), make_item(600000)] + FakeCatch.items
        self.sent, self.sheet = [], []
        ca.run(self.cfg, state)
        self.assertEqual(len(self.sent), 3)
        self.assertIn("회사600000", self.sent[0])
        self.assertIn("&lt;600000&gt;", self.sent[0])   # HTML 이스케이프 유지
        self.assertEqual([p["link"].rsplit("/", 1)[1] for p in self.sheet], ["600000", "600001", "600002"])
        self.assertEqual(state.get("transport"), "direct")
        # 다시 실행하면 보낼 것 없음
        self.sent = []
        ca.run(self.cfg, state)
        self.assertEqual(self.sent, [])

    def test_fresh_seed_skipped_when_partial(self):
        FakeCatch.fail_pages = {2}
        ca.run(self.cfg, {})
        self.assertFalse(os.path.exists(ca.STATE_PATH))   # 일부만 받은 상태로 기준을 만들지 않음
        self.assertEqual(self.sent, [])

    def test_seen_saved_even_if_send_crashes(self):
        ca.run(self.cfg, {})
        FakeCatch.items = [make_item(700001), make_item(700000)] + FakeCatch.items
        calls = []

        def flaky(token, chat_ids, text, cfg):
            calls.append(text)
            if len(calls) == 2:
                raise RuntimeError("텔레그램 도중 예외")
            return True
        ca.notify_recipients = flaky
        with self.assertRaises(RuntimeError):
            ca.run(self.cfg, {})
        with open(ca.STATE_PATH, encoding="utf-8") as f:
            seen = set(json.load(f)["seen_ids"])
        self.assertIn("700000", seen)        # 먼저 보낸 1건은 기록됨(중복 전송 방지)
        self.assertNotIn("700001", seen)     # 실패한 건은 다음 실행에서 재시도


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
netkit 오프라인 테스트 — 로컬 가짜 사이트/프록시로 '경주·장애전환·풀 관리'를 검증합니다.
실행: python -m unittest discover -s tests -v   (리포 루트에서)
인터넷 연결 불필요(127.0.0.1 만 사용).
"""
import os
import sys
import time
import socket
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = "127.0.0.1,localhost"
os.environ.pop("NETKIT_FORCE_PROXY", None)

import netkit  # noqa: E402
from netkit import Net, FetchError, DIRECT  # noqa: E402

GOOD = "<html>SITE-MARKER GOOD-MARKER %s</html>"


def validate(status, text):
    if status in (403, 429):
        return "blocked", "HTTP %s" % status
    if status != 200:
        return "bad", "HTTP %s" % status
    if "GOOD-MARKER" in text:
        return "ok", text
    if "SITE-MARKER" in text:
        return "empty", "0건"
    return "bad", "사이트 아님"


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Target(_Quiet):
    geo_block = False      # True 면 프록시를 거치지 않은 요청은 403
    direct_delay = 0.0     # 프록시를 안 거친 요청 지연(초)
    hits = []

    def do_GET(self):
        via = self.headers.get("X-Via-Proxy")
        Target.hits.append((self.path, via))
        if not via and Target.direct_delay:
            time.sleep(Target.direct_delay)
        if Target.geo_block and not via:
            return self._send(403, "<html>forbidden</html>")
        if self.path.startswith("/empty"):
            return self._send(200, "<html>SITE-MARKER</html>")
        return self._send(200, GOOD % self.path)

    def _send(self, code, body):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def make_proxy(mode, name, delay=0.0, die_after=None, trickle_after=None):
    """mode: good | garbage | p403 | hang | trickle | slow"""
    state = {"n": 0}

    class Proxy(_Quiet):
        def do_GET(self):
            state["n"] += 1
            if die_after is not None and state["n"] > die_after:
                self.connection.shutdown(socket.SHUT_RDWR)   # 이후 요청은 연결을 끊어 버림
                return
            cur = mode
            if trickle_after is not None and state["n"] > trickle_after:
                cur = "trickle"
            if cur == "hang":
                time.sleep(30)
                return
            if cur == "p403":
                return self._send(403, b"proxy says no")
            if cur == "garbage":
                return self._send(200, b"<html>free wifi login</html>")
            if cur == "trickle":
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                for _ in range(60):
                    try:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    except Exception:
                        return
                    time.sleep(0.5)
                return
            if delay:
                time.sleep(delay)
            req = urllib.request.Request(self.path, headers={"X-Via-Proxy": name})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(req, timeout=10) as r:
                    return self._send(r.status, r.read())
            except urllib.error.HTTPError as e:
                return self._send(e.code, e.read())

        def _send(self, code, b):
            self.send_response(code)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = _serve(Proxy)
    return srv, "http://127.0.0.1:%d" % srv.server_address[1], state


def dead_proxy():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()   # 아무도 안 듣는 포트 → 즉시 연결 거부
    return "http://127.0.0.1:%d" % port


class NetkitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target = _serve(Target)
        cls.base = "http://127.0.0.1:%d" % cls.target.server_address[1]
        cls.servers = []

    @classmethod
    def tearDownClass(cls):
        cls.target.shutdown()
        for s in cls.servers:
            s.shutdown()

    def setUp(self):
        Target.geo_block = False
        Target.direct_delay = 0.0
        Target.hits = []
        self.fresh = []
        self._orig = netkit.fetch_proxy_candidates
        netkit.fetch_proxy_candidates = lambda sources=None, **k: (list(self.fresh), "test")
        self.logs = []

    def tearDown(self):
        netkit.fetch_proxy_candidates = self._orig

    def proxy(self, mode, name, **kw):
        srv, url, st = make_proxy(mode, name, **kw)
        self.servers.append(srv)
        return url, st

    def net(self, state=None, **kw):
        kw.setdefault("head_start", 0.5)
        kw.setdefault("proxy_timeout", (1, 2))
        kw.setdefault("direct_timeout", (1, 5))
        kw.setdefault("proxy_cap", 4)
        kw.setdefault("log", self.logs.append)
        return Net(state if state is not None else {}, site="테스트", **kw)

    # ── 1. 직접 연결이 되면 프록시는 아예 출발하지 않는다
    def test_direct_fast_no_proxies(self):
        good, st = self.proxy("good", "p1")
        n = self.net({"proxy_pool": [{"p": good, "ok": netkit._now_min(), "f": 0}]})
        t = time.monotonic()
        payload, via = n.get(self.base + "/a", validate)
        self.assertEqual(via, DIRECT)
        self.assertIn("GOOD-MARKER", payload)
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertEqual(n.diag["launched"], 1)
        self.assertEqual(st["n"], 0)

    # ── 2. 직접이 403(해외 IP 차단) → 즉시 프록시 경주, 풀의 정상 프록시가 이김
    def test_geo_block_pool_wins(self):
        Target.geo_block = True
        good, _ = self.proxy("good", "p-good")
        garbage, _ = self.proxy("garbage", "p-garbage")
        state = {"proxy_pool": [{"p": garbage, "ok": netkit._now_min(), "f": 0},
                                {"p": good, "ok": "2026-01-01 00:00", "f": 0}]}
        self.fresh = [dead_proxy(), dead_proxy()]
        n = self.net(state)
        t = time.monotonic()
        payload, via = n.get(self.base + "/a", validate)
        self.assertEqual(via, netkit.proxy_label(good))
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(n.diag["direct"], "blocked")
        self.assertIn(garbage, n.bad_proxies)

    # ── 3. 직접이 먹통(응답 지연) → head_start 후 프록시 출발, 프록시가 먼저 성공
    def test_direct_slow_proxy_overtakes(self):
        Target.direct_delay = 4.0
        good, _ = self.proxy("good", "p-good")
        self.fresh = [good]
        n = self.net(direct_timeout=(1, 10), direct_cap=10)
        t = time.monotonic()
        payload, via = n.get(self.base + "/a", validate)
        self.assertEqual(via, netkit.proxy_label(good))
        self.assertLess(time.monotonic() - t, 3.5)   # 4초 지연된 직접 연결을 기다리지 않음

    # ── 4. 직접과 프록시가 거의 동시에 성공하면 직접 연결을 우선
    def test_direct_preferred_within_grace(self):
        Target.direct_delay = 0.9
        good, _ = self.proxy("good", "p-good")
        n = self.net({"proxy_pool": [{"p": good, "ok": netkit._now_min(), "f": 0}]},
                     head_start=0.2, direct_timeout=(1, 5))
        payload, via = n.get(self.base + "/a", validate)
        self.assertEqual(via, DIRECT)
        self.assertEqual(n.backups, [good])

    # ── 5. 모두 실패 → 빠르게 FetchError(blocked)
    def test_all_fail_blocked(self):
        Target.geo_block = True
        p403, _ = self.proxy("p403", "p403")
        self.fresh = [dead_proxy(), dead_proxy(), p403]
        n = self.net()
        t = time.monotonic()
        with self.assertRaises(FetchError) as cm:
            n.get(self.base + "/a", validate, budget=20)
        self.assertEqual(cm.exception.kind, "blocked")
        self.assertLess(time.monotonic() - t, 5.0)

    # ── 6. 직접이 200인데 공고 0건 → 구조 변경 의심(structure)
    def test_structure_change(self):
        good, _ = self.proxy("good", "p-good")
        self.fresh = [good]
        n = self.net()
        with self.assertRaises(FetchError) as cm:
            n.get(self.base + "/empty", validate, budget=10)
        self.assertEqual(cm.exception.kind, "structure")

    # ── 7. 프록시 1개만 '빈 응답' → 구조 변경으로 단정하지 않음(network)
    def test_single_proxy_empty_is_not_structure(self):
        Target.geo_block = True
        self.fresh = []
        good, _ = self.proxy("good", "p-good")
        n = self.net(force_proxy=True, sources=[])
        n.pool = [{"p": good, "ok": netkit._now_min(), "f": 0}]
        with self.assertRaises(FetchError) as cm:
            n.get(self.base + "/empty", validate, budget=8)
        self.assertEqual(cm.exception.kind, "network")

    # ── 8. 쓰던 프록시가 실행 중 죽으면 예비 프록시로 전환
    def test_failover_to_backup(self):
        Target.geo_block = True
        flaky, _ = self.proxy("good", "p-flaky", die_after=1)
        backup, _ = self.proxy("good", "p-backup", delay=0.3)
        n = self.net({"proxy_pool": [{"p": flaky, "ok": netkit._now_min(), "f": 0},
                                     {"p": backup, "ok": netkit._now_min(), "f": 0}]})
        _, via1 = n.get(self.base + "/p1", validate)
        self.assertEqual(via1, netkit.proxy_label(flaky))
        self.assertEqual(n.backups, [backup])
        _, via2 = n.get(self.base + "/p2", validate)
        self.assertEqual(via2, netkit.proxy_label(backup))
        self.assertTrue(any("예비" in m for m in self.logs))

    # ── 9. 응답을 찔끔찔끔 주는 프록시는 시도 상한(cap)으로 끊고 다른 프록시 사용
    def test_trickle_capped(self):
        Target.geo_block = True
        trickle, _ = self.proxy("trickle", "p-trickle")
        self.fresh = [trickle]
        n = self.net(proxy_cap=2.0, proxy_timeout=(1, 3))
        t = time.monotonic()
        with self.assertRaises(FetchError):
            n.get(self.base + "/a", validate, budget=15)
        self.assertLess(time.monotonic() - t, 4.0)      # 30초짜리 찔끔 응답을 끝까지 기다리지 않음
        self.assertIn(trickle, n.bad_proxies)
        # 정상 프록시가 함께 있으면 그쪽이 이김
        Target.hits = []
        good, _ = self.proxy("good", "p-good", delay=0.5)
        self.fresh = [trickle, good]
        n2 = self.net(proxy_cap=2.0, proxy_timeout=(1, 3))
        _, via = n2.get(self.base + "/a", validate, budget=15)
        self.assertEqual(via, netkit.proxy_label(good))

    # ── 9b. 쓰던 프록시가 2페이지부터 찔끔 응답 → 감시가 끊고 예비로 전환(멈추지 않음)
    def test_sticky_winner_turns_trickle(self):
        Target.geo_block = True
        flaky, _ = self.proxy("good", "p-flaky", trickle_after=1)
        backup, _ = self.proxy("good", "p-backup", delay=0.3)
        n = self.net({"proxy_pool": [{"p": flaky, "ok": netkit._now_min(), "f": 0},
                                     {"p": backup, "ok": netkit._now_min(), "f": 0}]},
                     proxy_cap=2.0, proxy_timeout=(1, 3))
        _, via1 = n.get(self.base + "/p1", validate)
        self.assertEqual(via1, netkit.proxy_label(flaky))
        t = time.monotonic()
        _, via2 = n.get(self.base + "/p2", validate, budget=20)
        self.assertEqual(via2, netkit.proxy_label(backup))
        self.assertLess(time.monotonic() - t, 6.0)

    # ── 10. 승자가 정해지면 느린 나머지는 취소(실패로 기록하지 않음)
    def test_losers_cancelled_not_failed(self):
        Target.geo_block = True
        fast, _ = self.proxy("good", "p-fast")
        slow, _ = self.proxy("trickle", "p-slowtrickle")
        n = self.net({"proxy_pool": [{"p": slow, "ok": netkit._now_min(), "f": 0},
                                     {"p": fast, "ok": netkit._now_min(), "f": 0}]},
                     proxy_cap=20, proxy_timeout=(1, 5))
        _, via = n.get(self.base + "/a", validate)
        self.assertEqual(via, netkit.proxy_label(fast))
        self.assertNotIn(slow, n.bad_proxies)

    # ── 11. 새 후보 목록이 늦게 와도 기다렸다가 사용
    def test_waits_for_late_fresh_list(self):
        Target.geo_block = True
        good, _ = self.proxy("good", "p-good")

        def late(sources=None, **k):
            time.sleep(0.8)
            return [good], "late"
        netkit.fetch_proxy_candidates = late
        n = self.net()
        _, via = n.get(self.base + "/a", validate, budget=10)
        self.assertEqual(via, netkit.proxy_label(good))

    # ── 12. 풀 관리: 성공 추가 · 실패 누적 · 2회 실패 삭제 · 변화 없으면 state 그대로
    def test_pool_bookkeeping(self):
        Target.geo_block = True
        good, _ = self.proxy("good", "p-good")
        dead = dead_proxy()
        now = netkit._now_min()
        state = {"proxy_pool": [{"p": dead, "ok": now, "f": 1}], "last_proxy": "1.2.3.4:8080"}
        self.fresh = [good]
        n = self.net(state)
        n.get(self.base + "/a", validate)
        n.finalize()
        ps = [e["p"] for e in state["proxy_pool"]]
        self.assertIn(good, ps)                 # 새로 통한 프록시 추가
        self.assertNotIn(dead, ps)              # 두 번째 실패 → 삭제
        self.assertIn("http://1.2.3.4:8080", ps)  # 예전 last_proxy 이전
        self.assertNotIn("last_proxy", state)

        # 같은 프록시로 다시 성공 → 6시간 안이면 state 가 바뀌지 않아야 함(불필요한 커밋 방지)
        before = [dict(e) for e in state["proxy_pool"]]
        n2 = self.net(state)
        n2.get(self.base + "/b", validate)
        n2.finalize()
        self.assertEqual(state["proxy_pool"], before)

    # ── 13. 직접 연결만 쓴 실행은 풀/state 를 건드리지 않음
    def test_direct_run_leaves_state_untouched(self):
        state = {"proxy_pool": [{"p": "http://8.8.8.8:3128", "ok": netkit._now_min(), "f": 0}],
                 "transport": "direct"}
        snap = {k: (list(v) if isinstance(v, list) else v) for k, v in state.items()}
        n = self.net(state)
        n.get(self.base + "/a", validate)
        n.finalize()
        self.assertEqual(state, snap)

    # ── 14. 오래된 풀 항목(72시간 초과)은 정리
    def test_pool_ttl(self):
        state = {"proxy_pool": [{"p": "http://8.8.8.8:3128", "ok": "2020-01-01 00:00", "f": 0}]}
        n = self.net(state)
        n.finalize()
        self.assertNotIn("proxy_pool", state)

    # ── 15. NETKIT_FORCE_PROXY: 직접 연결 생략
    def test_force_proxy(self):
        good, _ = self.proxy("good", "p-good")
        self.fresh = [good]
        n = self.net(force_proxy=True)
        _, via = n.get(self.base + "/a", validate)
        self.assertEqual(via, netkit.proxy_label(good))
        self.assertFalse(any(v is None for _, v in Target.hits))   # 직접 요청 0건

    # ── 16. 목록 파서: 형식/프로토콜/사설 IP 처리
    def test_parse_sources(self):
        class R(object):
            text = ("http://1.2.3.4:8080\nsocks5://5.6.7.8:1080\n10.0.0.1:3128\n"
                    "9.9.9.9:80:KR:elite\nhttps://4.4.4.4:443\nbad line\nsocks4://7.7.7.7:4145\n")
        got = netkit._parse_source("lines", R())
        self.assertIn("http://1.2.3.4:8080", got)
        self.assertIn("http://9.9.9.9:80", got)
        self.assertIn("http://4.4.4.4:443", got)
        self.assertNotIn("http://10.0.0.1:3128", got)   # 사설 IP 제외
        if netkit.HAS_SOCKS:
            self.assertIn("socks5://5.6.7.8:1080", got)
            self.assertIn("socks4://7.7.7.7:4145", got)

        class G(object):
            def json(self):
                return {"data": [
                    {"ip": "1.1.1.1", "port": "80", "protocols": ["http"], "upTime": 10},
                    {"ip": "2.2.2.2", "port": "8080", "protocols": ["https"], "upTime": 99},
                    {"ip": "3.3.3.3", "port": "1080", "protocols": ["socks5"], "upTime": 50}]}
        got = netkit._parse_source("geonode", G())
        self.assertEqual(got[0], "http://2.2.2.2:8080")   # 가동률 높은 것 먼저
        self.assertEqual(netkit.proxy_label("http://2.2.2.2:8080"), "proxy:2.2.2.2:8080")
        self.assertEqual(netkit.proxy_label("socks5://3.3.3.3:1080"), "proxy:socks5://3.3.3.3:1080")


if __name__ == "__main__":
    unittest.main(verbosity=2)

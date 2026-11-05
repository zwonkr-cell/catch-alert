# -*- coding: utf-8 -*-
"""
netkit — 채용 알림 봇(인크루트·잡코리아·캐치) 공통 네트워크 계층
====================================================================
incruit-bot · job-bot · catch-alert 세 리포에 **같은 파일**이 들어 있습니다.
고칠 때는 세 곳을 함께 맞추고 아래 __version__ 도 같이 올려 주세요.

[해결하려는 문제]
  GitHub Actions 러너(미국 Azure)에서 한국 사이트에 직접 접속하면 러너 IP 대역에 따라
  응답 없이 연결이 끊기거나(연결 타임아웃) 403 으로 막힙니다(캐치는 항상 403).
  예전 방식은 ① 직접 접속을 40초 타임아웃으로 3번(+8초 대기) 시도한 뒤 ② 무료 프록시를
  하나씩 10초씩 기다리며 차례로 시도해, 실패하는 실행은 2~6분을 쓰고도 빈손인 경우가 많았습니다.

[동작 방식]
  1) 경주(race): 직접 연결을 먼저 출발시키고, head_start(기본 2.5초) 안에 성공하지 못하거나
     실패하면 즉시 '기억해 둔 프록시(풀) + 무료 한국 프록시 후보'를 최대 32개까지 동시에
     출발시켜 가장 먼저 '정상 응답(검증 통과)'을 준 경로를 씁니다.
  2) 빠른 포기: 연결 타임아웃 직접 6초·프록시 4초, 시도 1건당 총 시간 상한(직접 35초·프록시 25초).
  3) 프록시 풀: 통했던 프록시를 state["proxy_pool"] 에 기억했다가 다음 실행에서 먼저 씁니다.
     실패가 이어지거나(2회) 오래된(72시간) 것은 자동으로 뺍니다. 커밋이 쓸데없이 늘지 않도록
     '의미 있는 변화'(추가·삭제·실패 누적·6시간 이상 지난 갱신)만 state 에 반영합니다.
  4) 장애 전환(failover): 실행 도중 쓰던 경로가 죽으면 예비 프록시 → 전체 재경주 순으로 갈아탑니다.
  5) 후보 확대: 무료 한국 프록시 목록 4곳을 병렬로 받고, SOCKS5/4 프록시도 씁니다(PySocks 필요).

[사용법]
    net = Net(state, headers=HEADERS, encoding="cp949", log=print, site="인크루트")
    payload, via = net.get(url, validate)   # via = "direct" 또는 "proxy:1.2.3.4:8080"
    ...
    net.finalize()                          # 프록시 풀을 state 에 반영(파일 저장은 봇이 함)
    print(net.summary())                    # 실행 요약 한 줄(로그용)

  validate(status, text) 는 아래 중 하나를 돌려줍니다(작업 스레드에서 호출되므로 순수 함수로).
    ("ok", 결과)       정상 응답. 결과를 그대로 돌려줌(조건에 맞는 공고가 정말 0건이면 빈 목록도 ok)
    ("empty", 사유)    진짜 사이트 응답인데 공고를 못 읽음 → '구조 변경 의심' 근거
    ("blocked", 사유)  403/429/캡차 등 차단
    ("bad", 사유)      사이트 응답이 아님(프록시 오류 페이지 등)

  모든 경로가 실패하면 FetchError(kind="structure"|"blocked"|"network") 를 던집니다.

  환경변수 NETKIT_FORCE_PROXY=1 이면 직접 연결을 건너뛰고 프록시만 씁니다(프록시 경로 점검용).
"""

import os
import re
import time
import queue
import ipaddress
import threading
from datetime import datetime, timezone, timedelta

import requests

try:
    import socks  # noqa: F401  (PySocks: requests 가 socks4/socks5 프록시를 쓰게 해 줌)
    HAS_SOCKS = True
except Exception:  # 없으면 HTTP 프록시만 사용
    HAS_SOCKS = False

__version__ = "2026.11.05"

KST = timezone(timedelta(hours=9))
DIRECT = "direct"

# 무료 한국 프록시 목록. 한 곳이 죽어도 나머지로 버팁니다.
# (2026-11 실측: geonode 40 · proxifly 110 · proxyscrape v4 30 · v2 5 → 중복 제외 약 150개,
#  이 중 절반 정도가 SOCKS 라서 PySocks 가 있으면 후보가 약 2배가 됩니다.)
KR_PROXY_SOURCES = [
    ("geonode",
     "https://proxylist.geonode.com/api/proxy-list?country=KR&limit=100&page=1"
     "&sort_by=lastChecked&sort_type=desc", "geonode"),
    ("proxifly",
     "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/KR/data.txt",
     "lines"),
    ("proxyscrape4",
     "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
     "&country=kr&protocol=all&proxy_format=protocolipport&format=text", "lines"),
    ("proxyscrape2",
     "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=kr",
     "lines"),
]

# "1.2.3.4:8080" · "http://1.2.3.4:8080" · "socks5://1.2.3.4:1080" · "1.2.3.4:8080:KR:..." 모두 허용
_LINE_RE = re.compile(r"^\s*(?:([A-Za-z0-9]+)://)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})(?!\d)")


# ─────────────────────────────────────────────────────────────
# 작은 도우미
# ─────────────────────────────────────────────────────────────
def _now_min():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def _parse_min(s):
    try:
        return datetime.strptime(str(s)[:16], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except Exception:
        return None


def _ts(s):
    dt = _parse_min(s)
    return dt.timestamp() if dt else 0.0


def _short_err(e):
    """requests 예외를 로그용 짧은 한국어로."""
    name = type(e).__name__
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return "연결 타임아웃"
    if isinstance(e, requests.exceptions.ReadTimeout):
        return "응답 타임아웃"
    if isinstance(e, requests.exceptions.ProxyError):
        return "프록시 연결 실패"
    if isinstance(e, requests.exceptions.SSLError):
        return "SSL 오류"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "연결 실패"
    msg = str(e).replace("\n", " ")
    return ("%s: %s" % (name, msg))[:160]


def normalize_proxy(scheme, ip, port):
    """(프로토콜, ip, port) → requests 용 프록시 URL. 쓸 수 없으면 None.
    'https' 표기는 'CONNECT 를 지원하는 HTTP 프록시'라는 뜻이라 http:// 로 넘깁니다."""
    s = (scheme or "http").strip().lower()
    if s in ("http", "https"):
        s = "http"
    elif s in ("socks5", "socks5h"):
        if not HAS_SOCKS:
            return None
        s = "socks5"
    elif s in ("socks4", "socks4a"):
        if not HAS_SOCKS:
            return None
        s = "socks4"
    else:
        return None
    try:
        port_i = int(port)
        if not 1 <= port_i <= 65535:
            return None
        if not ipaddress.ip_address(str(ip).strip()).is_global:
            return None
    except ValueError:
        return None
    return "%s://%s:%d" % (s, str(ip).strip(), port_i)


def proxy_label(t):
    """로그/state 표기: 'direct' · 'proxy:1.2.3.4:8080'(HTTP) · 'proxy:socks5://1.2.3.4:1080'"""
    if not t:
        return ""
    if t == DIRECT:
        return DIRECT
    return "proxy:" + (t[len("http://"):] if t.startswith("http://") else t)


def _parse_source(fmt, resp):
    out = []
    if fmt == "geonode":
        data = resp.json().get("data", []) or []

        def score(p):  # 가동률 높고 최근에 확인된 것부터
            try:
                up = float(p.get("upTime") or 0)
            except (TypeError, ValueError):
                up = 0.0
            try:
                chk = float(p.get("lastChecked") or 0)
            except (TypeError, ValueError):
                chk = 0.0
            return (-up, -chk)

        for p in sorted(data, key=score):
            for proto in (p.get("protocols") or ["http"]):
                u = normalize_proxy(proto, p.get("ip", ""), p.get("port"))
                if u:
                    out.append(u)
                    break
    else:
        for ln in resp.text.splitlines():
            m = _LINE_RE.match(ln)
            if m:
                u = normalize_proxy(m.group(1), m.group(2), m.group(3))
                if u:
                    out.append(u)
    return out


def fetch_proxy_candidates(sources=None, timeout=(4, 8), wait=14.0):
    """여러 무료 목록을 동시에 받아 합칩니다. 반환: (후보 URL 목록, 요약 문자열)"""
    sources = list(KR_PROXY_SOURCES if sources is None else sources)
    got = {}

    def one(src):
        name, url, fmt = src
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0 (job-alert-bot)"})
            if r.status_code != 200:
                got[name] = ([], "HTTP %s" % r.status_code)
                return
            got[name] = (_parse_source(fmt, r), "")
        except Exception as e:
            got[name] = ([], _short_err(e))

    ths = [threading.Thread(target=one, args=(s,), daemon=True) for s in sources]
    for t in ths:
        t.start()
    end = time.monotonic() + wait
    for t in ths:
        t.join(max(0.0, end - time.monotonic()))

    lists = [got.get(s[0], ([], ""))[0] for s in sources]
    merged, seen = [], set()
    for i in range(max([len(x) for x in lists] or [0])):   # 소스별로 번갈아(라운드로빈) 섞음
        for lst in lists:
            if i < len(lst) and lst[i] not in seen:
                seen.add(lst[i])
                merged.append(lst[i])
    parts = []
    for s in sources:
        lst, err = got.get(s[0], ([], "시간 초과"))
        parts.append("%s %d%s" % (s[0], len(lst), (" (%s)" % err) if err else ""))
    return merged, " · ".join(parts)


class FetchError(RuntimeError):
    """모든 경로가 실패. kind: 'structure'(구조 변경 의심) | 'blocked'(차단) | 'network'(접속 불가)"""

    def __init__(self, kind, message):
        RuntimeError.__init__(self, message)
        self.kind = kind


class _Cancelled(Exception):
    pass


class _Attempt(object):
    __slots__ = ("transport", "kind", "payload", "info", "status", "elapsed")

    def __init__(self, transport):
        self.transport = transport
        self.kind = "error"     # ok | empty | blocked | bad | error | cancelled
        self.payload = None
        self.info = ""
        self.status = None
        self.elapsed = 0.0


# ─────────────────────────────────────────────────────────────
# 본체
# ─────────────────────────────────────────────────────────────
class Net(object):
    def __init__(self, state=None, headers=None, encoding=None, log=print, site="사이트",
                 pool_key="proxy_pool", sources=None,
                 head_start=2.5, max_inflight=32, grace=0.8,
                 direct_timeout=(6, 20), proxy_timeout=(4, 12),
                 direct_cap=35.0, proxy_cap=25.0, max_bytes=8000000,
                 pool_max=12, pool_ttl_hours=72, refresh_minutes=360,
                 force_proxy=None):
        self.state = state if isinstance(state, dict) else {}
        self.headers = dict(headers or {})
        self.encoding = encoding
        self.log = log or (lambda *a, **k: None)
        self.site = site
        self.pool_key = pool_key
        self.sources = list(KR_PROXY_SOURCES if sources is None else sources)
        self.head_start = float(head_start)
        self.max_inflight = int(max_inflight)
        self.grace = float(grace)
        self.direct_timeout = direct_timeout
        self.proxy_timeout = proxy_timeout
        self.direct_cap = float(direct_cap)
        self.proxy_cap = float(proxy_cap)
        self.max_bytes = int(max_bytes)
        self.pool_max = int(pool_max)
        self.pool_ttl_hours = float(pool_ttl_hours)
        self.refresh_minutes = float(refresh_minutes)
        if force_proxy is None:
            force_proxy = os.environ.get("NETKIT_FORCE_PROXY", "").strip().lower() not in ("", "0", "false", "no")
        self.force_proxy = bool(force_proxy)

        self.failed = set()        # 이번 실행에서 실패한 경로(다시 쓰지 않음)
        if self.force_proxy:
            self.failed.add(DIRECT)
        self.ok_proxies = []       # 이번 실행에서 정상 응답을 준 프록시(발견 순)
        self.bad_proxies = set()   # 이번 실행에서 실패로 끝난 프록시
        self.winner = None         # 지금 쓰는 경로
        self.backups = []          # 예비 프록시(같은 경주에서 함께 성공한 것)
        self.requests_done = 0
        self.diag = {"direct": None, "direct_info": "", "empty": set(), "launched": 0}
        self._fresh = None         # 새 후보 목록(필요할 때 백그라운드로 수집)
        self._fresh_thread = None
        self._fresh_summary = ""
        self.pool = self._load_pool()

    # ── 풀 ──────────────────────────────────────────────────
    def _load_pool(self):
        pool = []
        raw = self.state.get(self.pool_key)
        if isinstance(raw, list):
            for e in raw:
                if isinstance(e, dict) and isinstance(e.get("p"), str) and e["p"]:
                    try:
                        f = int(e.get("f", 0) or 0)
                    except (TypeError, ValueError):
                        f = 0
                    pool.append({"p": e["p"], "ok": str(e.get("ok", ""))[:16], "f": f})
        # 예전 형식(last_proxy = "ip:port") → 풀로 옮김
        lp = self.state.get("last_proxy")
        if isinstance(lp, str) and lp.strip():
            m = _LINE_RE.match(lp.strip())
            u = normalize_proxy(m.group(1), m.group(2), m.group(3)) if m else None
            if u and all(e["p"] != u for e in pool):
                ok = str(self.state.get("last_proxy_at") or "")[:16] or _now_min()
                pool.append({"p": u, "ok": ok, "f": 0})
        return pool

    def _pool_order(self):
        ordered = sorted(self.pool, key=lambda e: (e["f"], -_ts(e["ok"])))
        return [e["p"] for e in ordered if e["p"] not in self.failed]

    def _ensure_fresh(self):
        if self._fresh_thread is not None:
            return

        def work():
            try:
                lst, summary = fetch_proxy_candidates(self.sources)
            except Exception as e:  # 방어: 어떤 경우에도 빈 목록으로 마무리
                lst, summary = [], "목록 수집 실패(%s)" % _short_err(e)
            self._fresh_summary = summary
            self._fresh = lst

        self._fresh_thread = threading.Thread(target=work, daemon=True)
        self._fresh_thread.start()

    # ── 한 번의 시도 ────────────────────────────────────────
    def _decode(self, content, resp):
        enc = self.encoding
        if not enc:
            m = re.search(r"charset=([\w.-]+)", resp.headers.get("Content-Type", ""), re.I)
            enc = m.group(1) if m else "utf-8"
        try:
            return content.decode(enc, errors="replace")
        except LookupError:
            return content.decode("utf-8", errors="replace")

    def _download(self, transport, url, stop, t0):
        direct = transport == DIRECT
        timeout = self.direct_timeout if direct else self.proxy_timeout
        cap = self.direct_cap if direct else self.proxy_cap
        s = requests.Session()
        try:
            if not direct:
                s.trust_env = False   # 환경변수 프록시는 무시하고 지정한 프록시만 사용
                s.proxies.update({"http": transport, "https": transport})
            r = s.get(url, headers=self.headers, timeout=timeout, stream=True)
            try:
                buf = bytearray()
                for chunk in r.iter_content(chunk_size=32768):
                    if stop.is_set():
                        raise _Cancelled()
                    if chunk:
                        buf.extend(chunk)
                        if len(buf) > self.max_bytes:
                            raise ValueError("응답이 너무 큼(%d bytes)" % len(buf))
                    if time.monotonic() - t0 > cap:
                        raise TimeoutError("시도 시간 상한 %d초 초과" % cap)
                return r.status_code, self._decode(bytes(buf), r)
            finally:
                r.close()
        finally:
            s.close()

    def _attempt(self, transport, url, validate, stop, results):
        att = _Attempt(transport)
        t0 = time.monotonic()
        try:
            status, text = self._download(transport, url, stop, t0)
            att.status = status
            try:
                kind, payload = validate(status, text)
            except Exception as e:  # 파서 예외 → 200 이면 구조 변경 근거로 취급
                kind = "empty" if status == 200 else "bad"
                payload = "검증 중 예외 %s: %s" % (type(e).__name__, e)
            att.kind = kind if kind in ("ok", "empty", "blocked", "bad") else "bad"
            if att.kind == "ok":
                att.payload = payload
            else:
                att.info = str(payload)[:160]
        except _Cancelled:
            att.kind = "cancelled"
        except Exception as e:
            att.kind = "error"
            att.info = _short_err(e)
        att.elapsed = time.monotonic() - t0
        results.put(att)

    def _note(self, att):
        """시도 결과를 이번 실행 기록에 반영(메인 스레드에서만 호출)."""
        if att.kind == "ok":
            if att.transport != DIRECT and att.transport not in self.ok_proxies:
                self.ok_proxies.append(att.transport)
            return
        if att.kind == "cancelled":
            return
        self.failed.add(att.transport)
        if att.transport == DIRECT:
            kind = att.kind
            if kind == "bad" and att.status == 200:
                # 직접 연결로 200 을 받았는데 목록을 못 읽음 → 구조 변경 근거(예전 봇과 같은 기준)
                kind = "empty"
            self.diag["direct"] = kind
            self.diag["direct_info"] = att.info or (("HTTP %s" % att.status) if att.status else "")
        else:
            self.bad_proxies.add(att.transport)
        if att.kind == "empty":
            self.diag["empty"].add(att.transport)

    def _cap(self, transport):
        return self.direct_cap if transport == DIRECT else self.proxy_cap

    # ── 경주 ────────────────────────────────────────────────
    def _race(self, url, validate, budget, only=None):
        """후보들을 동시에 출발시켜 가장 먼저 검증을 통과한 시도를 돌려줍니다.
        반환: (winner_attempt|None, 함께 성공한 시도들, 걸린 초, 요약)
        - 각 시도는 별도 데몬 스레드에서 돌고, 이 루프는 결과만 기다립니다.
        - 응답을 찔끔찔끔 보내 읽기 타임아웃에 안 걸리는 프록시도 있어서, 시도별 상한(cap)+1초가
          지나면 이 루프가 '포기'로 처리합니다(스레드는 뒤에서 혼자 끝나고 결과는 무시)."""
        t0 = time.monotonic()
        end = t0 + max(1.0, float(budget))
        stop = threading.Event()
        results = queue.Queue()
        inflight = {}
        abandoned = set()
        pending = []
        queued = set()
        n_pool = n_fresh = 0
        last_info = ""

        def enqueue(cands):
            n = 0
            for c in cands:
                if c not in queued and c not in self.failed:
                    queued.add(c)
                    pending.append(c)
                    n += 1
            return n

        if only is not None:          # 예비 프록시끼리만 짧게
            enqueue(only)
            proxies_open, fresh_merged, direct_racing = True, True, False
        else:
            proxies_open, fresh_merged = False, False
            direct_racing = enqueue([DIRECT]) > 0

        winner, extra, grace_until = None, [], 0.0
        while True:
            now = time.monotonic()
            if winner is not None:
                if now >= grace_until or not inflight:
                    break
            elif now >= end:
                break

            if winner is None:
                # 직접 연결이 head_start 안에 못 끝냈거나 실패 → 프록시 출발 허용
                if not proxies_open and (not direct_racing or now - t0 >= self.head_start):
                    proxies_open = True
                    n_pool = enqueue(self._pool_order())
                    self._ensure_fresh()
                if proxies_open and not fresh_merged and self._fresh is not None:
                    n_fresh = enqueue(self._fresh)
                    fresh_merged = True
                while pending and len(inflight) < self.max_inflight:
                    if pending[0] != DIRECT and not proxies_open:
                        break
                    c = pending.pop(0)
                    inflight[c] = now
                    self.diag["launched"] += 1
                    threading.Thread(target=self._attempt,
                                     args=(c, url, validate, stop, results), daemon=True).start()
                if not inflight and not pending:
                    if not proxies_open:
                        direct_racing = False      # 다음 바퀴에서 프록시 개방
                        continue
                    if not fresh_merged:
                        th = self._fresh_thread
                        if self._fresh is None and (th is None or not th.is_alive()):
                            fresh_merged = True    # 목록 수집이 비정상 종료 → 더 기다릴 것 없음
                            continue
                        # 새 후보 목록 도착 대기(아래 get 이 잠깐 기다림)
                    else:
                        break                      # 더 시도할 후보 없음

            # 감시: 상한을 넘긴 시도는 포기 처리
            now = time.monotonic()
            for c, started in list(inflight.items()):
                if now - started > self._cap(c) + 1.0:
                    inflight.pop(c, None)
                    abandoned.add(c)
                    dead = _Attempt(c)
                    dead.info = "시간 상한 초과(응답 지연)"
                    dead.elapsed = now - started
                    self._note(dead)
                    last_info = dead.info
                    if c == DIRECT:
                        direct_racing = False

            limit = (grace_until if winner is not None else end) - time.monotonic()
            try:
                att = results.get(timeout=min(0.25, max(0.01, limit)))
            except queue.Empty:
                continue
            if att.transport in abandoned:
                continue
            inflight.pop(att.transport, None)
            self._note(att)
            if att.kind not in ("ok", "cancelled"):
                last_info = att.info or att.kind
            if att.kind == "ok":
                if winner is None:
                    winner = att
                    if only is None:
                        # 직접 연결이 아직 달리는 중이면 조금 더 기다려 준다(직접이 더 빠르고 안정적)
                        g = max(self.grace, 2.0) if DIRECT in inflight else self.grace
                    else:
                        g = 0.0
                    grace_until = time.monotonic() + g
                else:
                    extra.append(att)
            elif att.transport == DIRECT:
                direct_racing = False
        stop.set()

        # 함께 성공한 것 중 직접 연결이 있으면 그것을 주 경로로
        if winner is not None and winner.transport != DIRECT:
            for a in list(extra):
                if a.transport == DIRECT:
                    extra.remove(a)
                    extra.insert(0, winner)
                    winner = a
                    break
        summary = {"pool": n_pool, "fresh": n_fresh, "launched": len(queued) - len(pending),
                   "fresh_summary": self._fresh_summary, "last_info": last_info}
        return winner, extra, time.monotonic() - t0, summary

    # ── 공개 API ────────────────────────────────────────────
    @property
    def label(self):
        return proxy_label(self.winner)

    def get(self, url, validate, budget=90.0):
        """url 을 가장 빠른 정상 경로로 받아 validate 결과(payload)와 경로 표기를 돌려줍니다."""
        t0 = time.monotonic()
        self.requests_done += 1
        # 1) 이번 실행에서 이미 통한 경로를 그대로(같은 사이트의 다음 페이지 등)
        if self.winner is not None:
            cur = self.winner
            w, _, _, sm = self._race(url, validate, self._cap(cur) + 2.0, only=[cur])
            if w is not None:
                return w.payload, self.label
            self.log("[net] %s 실패(%s) → 예비 경로로 전환" % (self.label, sm.get("last_info") or "실패"))
            self.winner = None
        # 2) 예비 프록시끼리 짧은 경주
        cands = [b for b in self.backups if b not in self.failed]
        self.backups = []
        if cands:
            w, extra, dt, sm = self._race(url, validate, min(25.0, budget), only=cands)
            if w is not None:
                self._adopt(w, extra, dt, sm, failover=True)
                # 나머지 예비(취소됐을 뿐 실패하지 않은 것)는 계속 예비로 둔다
                self.backups = [b for b in cands if b != self.winner and b not in self.failed]
                return w.payload, self.label
        # 3) 전체 경주(직접 + 풀 + 새 후보)
        remaining = float(budget) - (time.monotonic() - t0)
        w, extra, dt, sm = self._race(url, validate, max(15.0, remaining))
        if w is not None:
            self._adopt(w, extra, dt, sm)
            return w.payload, self.label
        err = self._error()
        self.log("[net] %s 수집 실패(%s): 직접=%s / 프록시 %d개 시도 · %s" % (
            self.site, err.kind, self._direct_desc(), len(self.bad_proxies),
            sm.get("fresh_summary") or "목록 미수집"))
        raise err

    def _direct_desc(self):
        if self.force_proxy:
            return "생략(NETKIT_FORCE_PROXY)"
        if self.diag["direct"] is None:
            return "미완료"
        return "%s(%s)" % (self.diag["direct"], self.diag["direct_info"] or "-")

    def _adopt(self, w, extra, dt, sm, failover=False):
        self.winner = w.transport
        self.backups = [a.transport for a in extra if a.transport != DIRECT]
        if w.transport == DIRECT:
            self.log("[net] 직접 연결 성공 %.1fs" % w.elapsed)
            return
        how = "예비 프록시로 전환" if failover else "프록시 경주"
        self.log("[net] 직접=%s → %s: 풀 %d·신규 %d개 중 %d개 출발, %.1fs 만에 %s 성공(예비 %d)%s" % (
            self._direct_desc(), how, sm.get("pool", 0), sm.get("fresh", 0), sm.get("launched", 0),
            dt, proxy_label(w.transport), len(self.backups),
            (" · 목록: " + sm["fresh_summary"]) if sm.get("fresh_summary") else ""))

    def _error(self):
        d = self.diag
        proxy_empty = [t for t in d["empty"] if t != DIRECT]
        if d["direct"] == "empty" or len(proxy_empty) >= 2:
            return FetchError("structure", "페이지 응답은 정상(200)이지만 공고를 읽지 못했습니다 - 사이트 구조 변경 의심")
        if d["direct"] == "blocked":
            return FetchError("blocked", "접근 차단 의심(403 등)이며 프록시 우회도 실패 - 다음 실행에서 재시도")
        return FetchError("network", "%s 접속 실패(직접+프록시 모두) - 사이트 접근 불가" % self.site)

    def finalize(self):
        """이번 실행 결과를 state 에 반영(의미 있는 변화만). 파일 저장은 봇이 합니다."""
        now = datetime.now(KST)
        pool = {}
        for e in self.pool:
            pool[e["p"]] = dict(e)
        for p in self.ok_proxies:
            e = pool.get(p)
            if e is None:
                pool[p] = {"p": p, "ok": _now_min(), "f": 0}
            else:
                if e["f"]:
                    e["f"] = 0
                dt = _parse_min(e["ok"])
                if dt is None or now - dt >= timedelta(minutes=self.refresh_minutes):
                    e["ok"] = _now_min()
        for p in self.bad_proxies:
            if p in pool and p not in self.ok_proxies:
                pool[p]["f"] += 1
        keep = []
        for e in pool.values():
            dt = _parse_min(e["ok"])
            if e["f"] >= 2 or dt is None or now - dt > timedelta(hours=self.pool_ttl_hours):
                continue
            keep.append(e)
        keep.sort(key=lambda e: (e["f"], -_ts(e["ok"])))
        keep = keep[:self.pool_max]
        if keep:
            if keep != self.state.get(self.pool_key):
                self.state[self.pool_key] = keep
        else:
            self.state.pop(self.pool_key, None)
        for k in ("last_proxy", "last_proxy_at"):   # 예전 형식 정리(1회성)
            self.state.pop(k, None)
        self.pool = keep
        return self.state

    def summary(self):
        return "[net] 요약: 경로=%s · 요청 %d회 · 출발 %d건 · 프록시 성공 %d/실패 %d · 풀 %d개%s" % (
            self.label or "없음", self.requests_done, self.diag["launched"], len(self.ok_proxies),
            len(self.bad_proxies), len(self.pool), "" if HAS_SOCKS else " · (PySocks 없음: SOCKS 제외)")

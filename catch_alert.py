# -*- coding: utf-8 -*-
"""
캐치(catch.co.kr) 채용공고 → 텔레그램 알림
────────────────────────────────────────────────────────────
매시 정각 Windows 작업 스케줄러가 이 스크립트를 실행합니다.
설정한 필터(config.json 의 api_url)에 새로 올라온 공고만 골라
텔레그램으로 지정한 수신자들에게 알림을 보냅니다.

- 표준 라이브러리만 사용 (pip 설치 불필요)
- 이미 알린 공고는 seen_ids.json 에 기록해 중복 알림 방지
- 첫 실행은 "무음 시딩": 기존 공고를 알림 없이 등록만 함

사용법:
    python catch_alert.py            # 정상 실행 (스케줄러가 호출)
    python catch_alert.py --test     # 설정 확인용 테스트 메시지 전송
    python catch_alert.py --dry-run  # 전송/저장 없이 무엇을 보낼지 미리보기
    python catch_alert.py --resend-latest N   # 최근 공고 N건을 강제로 다시 전송(디버깅)
"""

import sys
import os
import re
import json
import time
import html
import logging
import argparse
import traceback
from datetime import datetime, timezone, timedelta
from urllib import request, parse
from urllib.error import URLError, HTTPError

import requests  # 캐치 수집(프록시 지원)용. 텔레그램 전송은 표준 urllib 유지.

try:
    import msvcrt  # Windows 단일 인스턴스 락용
except ImportError:
    msvcrt = None

# ─────────────────────────────────────────────────────────────
# 경로 / 상수
# ─────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH  = os.path.join(BASE_DIR, "seen_ids.json")
LOG_PATH    = os.path.join(BASE_DIR, "catch_alert.log")
LOCK_PATH   = os.path.join(BASE_DIR, ".catch_alert.lock")

KST = timezone(timedelta(hours=9))            # 한국은 서머타임 없음 → 고정 +9
WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/122.0.0.0 Safari/537.36")

DETAIL_URL_FMT = "https://www.catch.co.kr/NCS/RecruitInfoDetails/{rid}"
PAGE_SIZE = 100

# 한 번 실행에서 이보다 많은 '신규'가 잡히면, 오래된 것부터 이 개수만 보내고
# 나머지는 다음 실행으로 미룹니다(폭주 방지 · 절대 폐기하지 않음).
MAX_NOTIFY_PER_RUN_DEFAULT = 20


class TelegramAuthError(Exception):
    """봇 토큰이 잘못됨(HTTP 401). 전체 실행을 즉시 중단해야 함."""


# ─────────────────────────────────────────────────────────────
# 로깅
# ─────────────────────────────────────────────────────────────
def setup_logging():
    # 콘솔 인코딩 이슈(한글/이모지) 방지
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logger = logging.getLogger("catch")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    try:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        # 파일 핸들러 실패해도 콘솔 로깅은 살림
        print("로그 파일을 열 수 없습니다:", e)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


log = setup_logging()


# ─────────────────────────────────────────────────────────────
# 단일 인스턴스 락 (실행 겹침 방지)
# ─────────────────────────────────────────────────────────────
_lock_fh = None  # 프로세스 수명 동안 열어 둠


def acquire_single_instance_lock():
    """이미 다른 인스턴스가 돌고 있으면 False."""
    global _lock_fh
    if msvcrt is None:
        return True  # 비 Windows: 락 생략
    try:
        _lock_fh = open(LOCK_PATH, "w")
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


# ─────────────────────────────────────────────────────────────
# 설정 / 상태 파일
# ─────────────────────────────────────────────────────────────
def load_config():
    """
    설정 우선순위: 환경변수 > config.json
    - 로컬:  config.json 사용(또는 $env:BOT_TOKEN)
    - GitHub: Secret 을 BOT_TOKEN 등 환경변수로 주입 (토큰을 리포지토리에 두지 않음)
    """
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception as e:
            log.error("config.json 을 읽을 수 없습니다 (JSON 형식 오류): %s", e)
            sys.exit(1)

    # ── 환경변수(깃헙 Secret / 로컬 env)가 있으면 config.json 값을 덮어씀
    env_token = os.environ.get("BOT_TOKEN", "").strip()
    if env_token:
        cfg["bot_token"] = env_token
    env_chat = os.environ.get("CHAT_IDS", "").strip()
    if env_chat:
        cfg["chat_ids"] = [c for c in re.split(r"[,\s]+", env_chat) if c]
    env_api = os.environ.get("API_URL", "").strip()
    if env_api:
        cfg["api_url"] = env_api

    token = str(cfg.get("bot_token", "")).strip()
    chat_ids = cfg.get("chat_ids", [])

    placeholder = (not token) or token.startswith("여기에") or token == "PUT_YOUR_BOT_TOKEN_HERE"
    if placeholder:
        log.error("봇 토큰이 없습니다. GitHub 는 Secret(BOT_TOKEN), 로컬은 환경변수 BOT_TOKEN "
                  "또는 config.json 의 bot_token 에 BotFather 토큰을 넣어주세요.")
        sys.exit(1)
    if not isinstance(chat_ids, list) or len(chat_ids) == 0:
        log.error("chat_ids 가 비어있습니다 (config.json 또는 환경변수 CHAT_IDS).")
        sys.exit(1)

    # chat_id 는 문자열로 통일 (텔레그램은 숫자/문자 모두 허용)
    cfg["chat_ids"] = [str(c).strip() for c in chat_ids if str(c).strip()]
    cfg.setdefault("api_url", "")
    cfg.setdefault("request_timeout", 30)
    cfg.setdefault("disable_web_page_preview", True)
    cfg.setdefault("weekday_full", True)   # True=화요일, False=화
    cfg.setdefault("send_summary_on_first_run", True)
    cfg.setdefault("max_notify_per_run", MAX_NOTIFY_PER_RUN_DEFAULT)
    if not cfg["api_url"]:
        log.error("config.json 의 api_url 이 비어있습니다.")
        sys.exit(1)
    return cfg


def load_seen():
    """반환: (seen_set, is_fresh)  is_fresh=True 이면 첫 실행/상태손상 → 무음 시딩 대상"""
    if not os.path.exists(STATE_PATH):
        return set(), True
    try:
        with open(STATE_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        ids = data.get("seen_ids", []) if isinstance(data, dict) else data
        return set(str(x) for x in ids), False
    except Exception as e:
        log.warning("seen_ids.json 을 읽을 수 없어 재시딩합니다 (기존 손상): %s", e)
        return set(), True


def save_seen(seen_set):
    """
    원자적 저장: 임시파일에 쓰고 교체 → 중간 크래시에도 파일 손상 방지.
    Windows 파일 잠금(백신/OneDrive 동기화)로 인한 일시 실패는 몇 차례 재시도.
    반환: 성공 True / 실패 False
    """
    payload = {
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(seen_set),
        "seen_ids": sorted(seen_set, key=lambda x: (len(x), x)),
    }
    tmp = STATE_PATH + ".tmp"
    last_err = None
    for attempt in range(1, 4):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_PATH)
            return True
        except OSError as e:   # PermissionError 포함
            last_err = e
            time.sleep(1.0 * attempt)
    log.error("상태 저장 실패(권한/파일잠금 의심): %s → 다음 실행에서 재시도됩니다.", last_err)
    return False


# ─────────────────────────────────────────────────────────────
# 봇 상태(bot_state.json): 12h 무신규 하트비트 + 오류 중복 알림 방지용 타임스탬프
# ─────────────────────────────────────────────────────────────
STATE2_PATH = os.path.join(BASE_DIR, "bot_state.json")
HEARTBEAT_HOURS = 12       # 이 시간 동안 새 공고가 없으면 '신규 없음' 노티
ERROR_DEDUP_HOURS = 12     # 같은 오류는 이 시간에 한 번만 텔레그램 노티(스팸 방지)


def load_bot_state():
    if os.path.exists(STATE2_PATH):
        try:
            with open(STATE2_PATH, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}


def save_bot_state(state):
    tmp = STATE2_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE2_PATH)
    except OSError as e:
        log.error("bot_state 저장 실패: %s", e)


def _now_iso():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except Exception:
        return None


SHEET_WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL", "").strip()


def log_to_sheet(payload):
    """새 공고 1건을 구글 시트(Apps Script 웹앱)에 기록. 실패해도 봇 동작엔 영향 없음."""
    if not SHEET_WEBHOOK_URL:
        return
    try:
        requests.post(SHEET_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        log.warning("구글시트 기록 실패: %s", e)


def _plain_send_all(cfg, text):
    """모든 수신자에게 일반텍스트로 전송(오류/하트비트 노티용). 성공 수 반환."""
    ok = 0
    for cid in cfg["chat_ids"]:
        try:
            sent, _, _ = _post_telegram(cfg["bot_token"], cid, text[:3900], cfg, use_html=False)
            ok += 1 if sent else 0
        except Exception as e:
            log.warning("노티 전송 실패 chat_id=%s: %s", cid, e)
    return ok


# ── 오류 분류: (키, 이모지, 심각도, 알림까지 필요한 연속횟수, 키워드, 제목, 원인, 조치) ──
ERROR_CATEGORIES = [
    ("structure", "🔴", "높음 · 조치 필요", 1,
     ["구조 변경", "파싱 0건", "attributeerror", "keyerror", "indexerror", "nonetype",
      "jsondecodeerror"],
     "사이트 구조/응답 변경 의심 — 공고를 읽지 못했어요",
     "캐치가 API 나 페이지를 개편하면 봇이 공고를 찾지 못하게 돼요.",
     "네, 코드 수정이 필요해요. 이 알림 내용을 개발 세션(클로드)에 전달해 주세요. 수정 전까지 새 공고 알림이 중단돼요."),
    ("proxy", "🟡", "낮음 · 조치 불필요", 2,
     ["프록시를 찾지 못했습니다", "프록시"],
     "한국 프록시를 일시적으로 찾지 못했어요",
     "캐치는 해외 서버를 차단해서 무료 한국 프록시로 우회하는데, 무료 프록시가 일시적으로 모두 죽어있을 때가 있어요.",
     "아니요. 다음 실행에서 새 프록시를 자동으로 찾고, 놓친 공고도 그대로 알림돼요. 다만 이 알림이 여러 날 반복되면 개발 세션에 전달해 주세요."),
    ("blocked", "🟠", "중간 · 지켜보기", 1,
     ["403", "forbidden", "captcha", "차단"],
     "사이트가 접근을 차단했을 가능성",
     "사이트가 자동 수집을 일시적으로 막았을 수 있어요.",
     "당장 조치는 필요 없어요. 이 알림이 하루 이상 반복되면 개발 세션에 전달해 주세요."),
    ("network", "🟡", "낮음 · 조치 불필요", 2,
     ["접속 5회 모두 실패", "connection", "timeout", "timed out", "10054", "reset",
      "aborted", "urlerror", "수집 실패"],
     "일시적 접속 장애",
     "서버 혼잡이나 순간적인 네트워크 문제로 가끔 발생해요.",
     "아니요. 다음 실행에서 자동 복구되고, 놓친 공고도 그대로 알림돼요."),
    ("state", "🟠", "중간 · 지켜보기", 1,
     ["oserror", "permissionerror", "json.decoder", "상태 저장"],
     "기록 파일 저장/읽기 문제",
     "공고 기록 파일을 읽거나 쓰는 데 문제가 생겼어요.",
     "일시적일 수 있어요. 반복되면 개발 세션에 전달해 주세요."),
]


def classify_error(raw_text):
    low = (raw_text or "").lower()
    for key, emoji, sev, min_consec, kws, title, why, action in ERROR_CATEGORIES:
        if any(k in low for k in kws):
            return {"key": key, "emoji": emoji, "sev": sev, "min_consec": min_consec,
                    "title": title, "why": why, "action": action}
    return {"key": "unknown", "emoji": "🔴", "sev": "높음 · 조치 필요", "min_consec": 1,
            "title": "알 수 없는 오류",
            "why": "예상하지 못한 문제가 발생했어요.",
            "action": "네, 확인이 필요해요. 이 알림 내용을 개발 세션(클로드)에 전달해 주세요."}


def notify_error(cfg, state, raw_text):
    """오류를 분류해 쉬운 설명으로 노티. 일시적 오류는 연속 2회부터, 같은 유형은 12h 1회."""
    raw_text = (raw_text or "").strip() or "알 수 없는 오류"
    summary = raw_text.splitlines()[-1][:100]
    info = classify_error(raw_text)
    consec = state.setdefault("consec_err", {})
    cnt = consec.get(info["key"], 0) + 1
    consec[info["key"]] = cnt
    if cnt < info["min_consec"]:
        log.info("[%s] 1회성 오류 → 알림 보류(연속 %d회부터 알림)", info["key"], info["min_consec"])
        return
    last_at = _parse_iso(state.get("err_notified_at", {}).get(info["key"]))
    if last_at and (datetime.now(KST) - last_at) < timedelta(hours=ERROR_DEDUP_HOURS):
        log.info("[%s] 같은 유형 최근 알림됨 → 생략(스팸 방지)", info["key"])
        return
    consec_note = f" (연속 {cnt}회째)" if cnt >= 2 else ""
    # 새로운(미분류) 유형은 판단 근거가 없으므로 오류 원문을 함께 첨부
    if info["key"] == "unknown":
        tail = f"■ 오류 원문 (처음 보는 유형이라 원문을 함께 보내요)\n{raw_text[:1500]}\n"
    else:
        tail = f"(참고: {summary})\n"
    msg = (f"{info['emoji']} [캐치봇] 오류 알림 — 심각도: {info['sev']}\n"
           f"\n"
           f"■ 무슨 오류인가요?\n{info['title']}{consec_note}\n"
           f"\n"
           f"■ 왜 발생하나요?\n{info['why']}\n"
           f"\n"
           f"■ 조치가 필요한가요?\n{info['action']}\n"
           f"\n"
           f"{tail}"
           f"(발생 시각: {_now_iso()})")
    _plain_send_all(cfg, msg)
    state.setdefault("err_notified_at", {})[info["key"]] = _now_iso()


def note_activity(state):
    """새 공고 전송 등 '활동'이 있었음을 기록(하트비트 타이머 리셋)."""
    state["last_activity_at"] = _now_iso()


def maybe_heartbeat(cfg, state, collected, new_count):
    """마지막 활동 후 HEARTBEAT_HOURS 지나도록 새 공고가 없으면 '신규 없음' 노티."""
    now = datetime.now(KST)
    last = _parse_iso(state.get("last_activity_at"))
    if last is None:
        state["last_activity_at"] = _now_iso()   # 최초: 타이머 시작(즉시 노티 방지)
        return
    if (now - last) >= timedelta(hours=HEARTBEAT_HOURS):
        msg = (f"⏰ [캐치봇] 최근 {HEARTBEAT_HOURS}시간 내 새로 스크래핑된 채용공고가 없어요.\n"
               f"수집완료 {collected}건, 신규후보 {new_count}건\n"
               f"(확인 시각: {_now_iso()})")
        _plain_send_all(cfg, msg)
        state["last_activity_at"] = _now_iso()


# ─────────────────────────────────────────────────────────────
# 일일 리포트: 매일 18시(KST) 이후 첫 실행 시, 전일 18시~당일 18시 신규 공고 요약
# ─────────────────────────────────────────────────────────────
REPORT_HOUR = 18           # KST 18시
SENT_LOG_KEEP_HOURS = 48   # sent_log 보관 기간(리포트 24h 윈도우 여유 있게)


def record_sent(state, company):
    """전송한 신규 공고의 기업명+시각을 일일 리포트용으로 기록."""
    state.setdefault("sent_log", []).append(
        {"company": (company or "(기업명 없음)"), "at": _now_iso()})


def prune_sent_log(state, keep_hours=SENT_LOG_KEEP_HOURS):
    now = datetime.now(KST)
    kept = []
    for e in state.get("sent_log", []):
        t = _parse_iso(e.get("at"))
        if t and (now - t) <= timedelta(hours=keep_hours):
            kept.append(e)
    state["sent_log"] = kept


def maybe_daily_report(cfg, state, now=None):
    """매일 REPORT_HOUR 이후 첫 실행에서 하루 요약 리포트 1회 발송. (now: 테스트용 주입)"""
    now = now or datetime.now(KST)
    today_1800 = now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    today_str = now.strftime("%Y-%m-%d")

    # 최초 실행: 이미 18시가 지났으면 오늘 리포트는 데이터가 없어 건너뜀(다음날부터)
    if not state.get("report_initialized"):
        state["report_initialized"] = True
        if now >= today_1800:
            state["last_report_date"] = today_str
        return

    if now < today_1800 or state.get("last_report_date") == today_str:
        return

    win_start = today_1800 - timedelta(days=1)
    companies = []
    n = 0
    for e in state.get("sent_log", []):
        t = _parse_iso(e.get("at"))
        if t and win_start <= t < today_1800:
            n += 1
            c = e.get("company") or "(기업명 없음)"
            if c not in companies:
                companies.append(c)

    header = (f"📊 [캐치봇] 일일 리포트\n"
              f"({win_start.strftime('%m/%d %H:%M')} ~ {today_1800.strftime('%m/%d %H:%M')})")
    if n:
        body = (f"\n신규 공고 {n}건 · 기업 {len(companies)}곳\n\n"
                + "\n".join(f"• {c}" for c in companies))
    else:
        body = "\n이 기간에 새로 올라온 공고가 없었어요."
    _plain_send_all(cfg, header + body)
    state["last_report_date"] = today_str
    note_activity(state)   # 리포트도 '활동' → 하트비트 타이머 리셋


# ─────────────────────────────────────────────────────────────
# HTTP: 직접 연결 → 실패(해외 IP 403) 시 한국 프록시 자동 탐색
#   catch.co.kr 는 해외/클라우드 IP 를 403 으로 차단합니다.
#   - 로컬(한국 IP): 직접 연결로 동작 (프록시 불필요)
#   - GitHub Actions(해외 IP): 매 실행마다 최신 무료 한국 프록시 목록을 받아
#     캐치에 실제로 통하는 프록시를 찾아 그 실행 내내 사용합니다.
#   프록시를 못 찾으면 이번 실행은 실패(exit 1)로 표시되고, 다음 정각에 재시도합니다.
#   (누락 없음: seen_ids 로 중복 관리하므로 놓친 공고는 다음 성공 실행에서 알림)
# ─────────────────────────────────────────────────────────────
REQ_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Referer": "https://www.catch.co.kr/",
    "Accept": "application/json, text/plain, */*",
}
PROXY_SOURCES = [
    "https://proxylist.geonode.com/api/proxy-list?country=KR&protocols=http%2Chttps"
    "&limit=100&page=1&sort_by=lastChecked&sort_type=desc",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=kr",
]
MAX_PROXY_TRIES = 40
PROXY_TIMEOUT = 8


def _small_test_url(cfg):
    parts = parse.urlsplit(cfg["api_url"])
    qs = dict(parse.parse_qsl(parts.query, keep_blank_values=True))
    qs["pageSize"] = "5"
    qs["curpage"] = "1"
    return parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                             parse.urlencode(qs), parts.fragment))


def get_kr_proxy_candidates():
    cands = []
    for url in PROXY_SOURCES:
        try:
            r = requests.get(url, timeout=15)
            if "geonode" in url:
                for p in r.json().get("data", []):
                    cands.append(f"{p['ip']}:{p['port']}")
            else:
                cands += [ln.strip() for ln in r.text.splitlines() if ":" in ln]
        except Exception as e:
            log.warning("프록시 목록 수집 실패(%s): %s", url[:45], e)
    return list(dict.fromkeys(cands))  # 순서 유지 dedupe


LAST_TRANSPORT = "direct"   # 이번 실행의 연결 방식 ("direct" | "proxy:IP")


def resolve_transport(cfg):
    """반환: proxies dict(프록시 사용) 또는 None(직접 연결). 둘 다 불가하면 예외."""
    global LAST_TRANSPORT
    test_url = _small_test_url(cfg)
    # 1) 직접 연결 (로컬 한국 IP 는 이걸로 끝)
    try:
        r = requests.get(test_url, headers=REQ_HEADERS, timeout=cfg["request_timeout"])
        if r.status_code == 200 and "recruitData" in r.text:
            log.info("직접 연결 성공 (프록시 불필요)")
            LAST_TRANSPORT = "direct"
            return None
        log.warning("직접 연결 status=%s → 한국 프록시 탐색", r.status_code)
    except Exception as e:
        log.warning("직접 연결 실패(%s) → 한국 프록시 탐색", e)
    # 2) 한국 프록시 탐색
    cands = get_kr_proxy_candidates()
    log.info("한국 프록시 후보 %d개 중 통하는 것 탐색...", len(cands))
    for p in cands[:MAX_PROXY_TRIES]:
        prox = {"http": f"http://{p}", "https": f"http://{p}"}
        try:
            r = requests.get(test_url, headers=REQ_HEADERS, proxies=prox, timeout=PROXY_TIMEOUT)
            if r.status_code == 200 and "recruitData" in r.text:
                log.info("프록시 사용: %s", p)
                LAST_TRANSPORT = f"proxy:{p}"
                return prox
        except Exception:
            continue
    raise RuntimeError("작동하는 한국 프록시를 찾지 못했습니다(캐치 접근 불가).")


def handle_transport(cfg, state):
    """
    연결 방식 변화를 감지해 노티.
    캐치는 GitHub(해외 IP)에서 프록시 우회가 '정상 상태'이므로,
    최초 실행은 조용히 기록만 하고 이후 '변화'가 있을 때만 알립니다.
    """
    cur = LAST_TRANSPORT
    prev = state.get("transport")
    if prev is None:
        state["transport"] = cur
        return
    cur_p = cur.startswith("proxy")
    prev_p = str(prev).startswith("proxy")
    if cur_p and not prev_p:
        _plain_send_all(cfg, "🛡 [캐치봇] 직접 연결이 차단되어 한국 프록시로 즉시 우회했어요.\n"
                             "수집·알림은 정상 작동 중이고, 조치는 필요 없어요.")
    elif (not cur_p) and prev_p:
        _plain_send_all(cfg, "✅ [캐치봇] 직접 연결이 복구되어 프록시 우회를 종료했어요.")
    state["transport"] = cur


def http_get_json(url, timeout, proxies=None, tries=3):
    last_err = None
    to = timeout if proxies is None else PROXY_TIMEOUT
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, headers=REQ_HEADERS, proxies=proxies, timeout=to)
            if r.status_code == 200:
                # BOM 방어
                return json.loads(r.content.decode("utf-8-sig", errors="replace"))
            if 400 <= r.status_code < 500 and r.status_code != 429:
                log.error("캐치 API HTTP %s (재시도 안 함)", r.status_code)
                raise RuntimeError(f"캐치 API HTTP {r.status_code}")
            log.warning("캐치 API 오류(HTTP %s), %d/%d 재시도", r.status_code, attempt, tries)
        except (requests.RequestException, json.JSONDecodeError) as e:
            last_err = e
            log.warning("캐치 API 네트워크/파싱 오류(%s), %d/%d 재시도", e, attempt, tries)
        if attempt < tries:
            time.sleep(2 ** attempt)   # 2s, 4s
    raise last_err or RuntimeError("캐치 수집 실패")


def fetch_all(cfg):
    """
    직접/프록시 경로를 정한 뒤, api_url 의 필터를 유지하되 pageSize/curpage 를
    조절해 전체 공고를 수집. intTotalRecordCount 를 맹신하지 않고, 덜 찬(또는 빈)
    페이지가 나오면 멈춘다는 규칙으로 견고하게 페이징.
    """
    transport = resolve_transport(cfg)   # None(직접) 또는 proxies dict

    parts = parse.urlsplit(cfg["api_url"])
    qs = dict(parse.parse_qsl(parts.query, keep_blank_values=True))
    qs["pageSize"] = str(PAGE_SIZE)

    items = []
    total = None
    for page in range(1, 21):  # 안전상 최대 20페이지(=2000건)
        qs["curpage"] = str(page)
        url = parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                parse.urlencode(qs), parts.fragment))
        data = http_get_json(url, cfg["request_timeout"], proxies=transport)
        batch = data.get("recruitData", []) or []
        if total is None:
            try:
                total = int(data.get("intTotalRecordCount", 0) or 0)
            except (TypeError, ValueError):
                total = 0
        items.extend(batch)
        # 페이지가 가득 차지 않았으면 마지막 페이지 → 종료
        if len(batch) < PAGE_SIZE:
            break
    return items, (total if total is not None else len(items))


# ─────────────────────────────────────────────────────────────
# 메시지 구성
# ─────────────────────────────────────────────────────────────
def esc(v):
    """None/빈값은 '-' 로, 나머지는 HTML 이스케이프."""
    if v is None:
        return "-"
    s = str(v).strip()
    return html.escape(s) if s else "-"


def format_deadline(item, weekday_full):
    """ApplyEndDatetime → 'MM/DD(요일)' (KST 기준). 실패 시 None."""
    raw = item.get("ApplyEndDatetime")
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(KST)
        wd = WEEKDAYS[dt.weekday()] if weekday_full else WEEKDAYS[dt.weekday()][0]
        return f"{dt.month:02d}/{dt.day:02d}({wd})"
    except Exception:
        return None


def build_close_line(item, deadline):
    """마감 안내 문구. Dday==0(오늘 마감)/음수(이미 마감)까지 구분."""
    dday = item.get("Dday")
    try:
        dday = int(dday) if dday is not None else None
    except (TypeError, ValueError):
        dday = None

    if dday is not None and dday <= 0:
        base = "오늘(마지막 날) 마감돼요!" if dday == 0 else "마감일이 지난 공고예요."
        return f"{base} {deadline}에 마감" if deadline else base
    if dday is not None and deadline:
        return f"마감까지 {dday}일 남았어요. {deadline}에 마감돼요."
    if deadline:
        return f"{deadline}에 마감돼요."
    if dday is not None:
        return f"마감까지 {dday}일 남았어요."
    return "마감일 정보가 없어요 (상시/채용시 마감)."


def build_message(item, cfg, scraped_ts):
    comp    = esc(item.get("CompName"))
    title   = esc(item.get("RecruitTitle"))
    area    = esc(item.get("WorkArea"))
    depth   = esc(item.get("Depth"))
    popular = esc(item.get("PopularCategory"))
    career  = esc(item.get("CareerGubunCode"))
    gubun   = esc(item.get("GubunCode"))
    categ   = esc(item.get("RecruitCategory"))

    rid = item.get("RecruitID")
    url = DETAIL_URL_FMT.format(rid=rid) if rid is not None else "https://www.catch.co.kr/"
    link = f'<a href="{html.escape(url)}">{title}</a>'

    close_line = build_close_line(item, format_deadline(item, cfg["weekday_full"]))

    lines = [
        f"<b>{comp} - {title}</b>",
        "",
        "[기본정보]",
        f"• {comp} ({area})",
        f"• {depth}",
        f"• {popular}",
        f"• {link}",
        "",
        "[추가정보]",
        f"• {career}",
        f"• {gubun}",
        f"• {categ}",
        "",
        close_line,
        f"본 데이터는 {scraped_ts}에 스크래핑 되어 사용자에게 제공돼요.",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 텔레그램 전송
# ─────────────────────────────────────────────────────────────
def _post_telegram(token, chat_id, text, cfg, use_html=True):
    """단일 POST. (ok:bool, http_code:int|None, description:str) 반환."""
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    fields = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true" if cfg["disable_web_page_preview"] else "false",
    }
    if use_html:
        fields["parse_mode"] = "HTML"
    payload = parse.urlencode(fields).encode("utf-8")
    req = request.Request(api, data=payload, headers={"User-Agent": "catch-alert/1.0"})
    try:
        with request.urlopen(req, timeout=cfg["request_timeout"]) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        return bool(body.get("ok")), 200, body.get("description", "")
    except HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            body = {}
        return False, e.code, body.get("description", str(e))


def tg_send(token, chat_id, text, cfg, tries=3):
    """
    반환:
      "ok"        전송 성공
      "permanent" 이 수신자에게 재시도 무의미(차단/미시작 등) → seen 처리 가능
      "transient" 일시 오류(네트워크/429/5xx/형식오류) → 다음 실행 때 재시도
    401(잘못된 토큰)은 TelegramAuthError 를 던져 전체 실행을 중단.
    """
    for attempt in range(1, tries + 1):
        try:
            ok, code, desc = _post_telegram(token, chat_id, text, cfg, use_html=True)
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            log.warning("텔레그램 네트워크 오류(%s), %d/%d 재시도", e, attempt, tries)
            if attempt < tries:
                time.sleep(2 * attempt)
            continue

        if ok:
            return "ok"

        dl = (desc or "").lower()

        # 성공(HTTP 200)했지만 ok:false 인 경우도 code=200 으로 들어옴
        if code == 401 or "unauthorized" in dl:
            raise TelegramAuthError(desc or "Unauthorized (bot token 오류)")

        if code == 429:
            # description 없이 code 만 온 경우 대비: 기본 3초
            retry_after = 3
            log.warning("텔레그램 429 Too Many Requests, %d초 대기", retry_after)
            time.sleep(retry_after)
            continue

        # 이 수신자에게 영구적으로 도달 불가 → seen 처리해도 됨(재시도 무의미)
        per_chat_fatal = (
            code == 403
            or "chat not found" in dl
            or "chat_id is empty" in dl
            or "peer_id_invalid" in dl
            or "bot can't initiate" in dl
            or "user is deactivated" in dl
            or "bot was blocked" in dl
        )
        if per_chat_fatal:
            log.error("텔레그램 영구 오류 chat_id=%s (HTTP %s): %s", chat_id, code, desc)
            return "permanent"

        # 메시지 형식 문제(파싱/길이 등): HTML 없이 1회 폴백 시도
        if code == 400:
            log.warning("텔레그램 400 (형식 의심) chat_id=%s: %s → 일반텍스트로 폴백 시도",
                        chat_id, desc)
            ok2, desc2 = False, desc
            try:
                ok2, _, desc2 = _post_telegram(token, chat_id, text, cfg, use_html=False)
            except (URLError, TimeoutError, json.JSONDecodeError):
                pass
            if ok2:
                return "ok"
            log.error("텔레그램 400 폴백도 실패 chat_id=%s: %s", chat_id, desc2)
            return "transient"   # seen 처리하지 않음 → 다음에 재시도

        # 기타 5xx 등 → 재시도
        log.warning("텔레그램 오류(HTTP %s), %d/%d 재시도: %s", code, attempt, tries, desc)
        if attempt < tries:
            time.sleep(2 * attempt)

    return "transient"


def notify_recipients(token, chat_ids, text, cfg):
    """
    모든 수신자에게 전송.
    반환 True 이면 '이 공고를 seen 처리해도 됨'.
    규칙: 모든 수신자가 ok 또는 permanent 여야 True.
          한 명이라도 transient(일시 오류)면 False → 다음 실행에서 전체 재시도.
    (드물게 '한 명 성공 + 다른 한 명 일시실패' 시 재시도로 인한 중복 전송 가능하나,
     영구 누락보다 훨씬 안전한 트레이드오프.)
    """
    results = []
    for i, cid in enumerate(chat_ids):
        results.append(tg_send(token, cid, text, cfg))
        if i < len(chat_ids) - 1:
            time.sleep(0.5)   # 초당 제한 여유
    return all(r in ("ok", "permanent") for r in results)


# ─────────────────────────────────────────────────────────────
# 메인 로직
# ─────────────────────────────────────────────────────────────
def item_id(item):
    return str(item.get("RecruitID"))


def sort_key_oldest_first(it):
    try:
        return int(item_id(it))
    except ValueError:
        return 0


def run(cfg, state, dry_run=False):
    scraped_ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    seen, is_fresh = load_seen()

    try:
        items, total = fetch_all(cfg)
    except Exception:
        # 프록시 포함 수집 실패 → main() 이 오류 원문을 텔레그램으로 노티하도록 재전파.
        # seen 은 그대로라 다음 성공 실행에서 놓친 공고까지 알림됨(누락 없음).
        log.error("공고 수집 실패(직접+프록시 모두)")
        raise

    log.info("수집 완료: %d건 (총 %d건 보고됨)", len(items), total)

    state["consec_err"] = {}   # 수집 성공 → 연속 오류 카운터 리셋
    if not dry_run:
        handle_transport(cfg, state)   # 연결 방식(직접↔프록시) 변화 노티

    valid = [it for it in items if it.get("RecruitID") is not None]
    current_ids = {item_id(it) for it in valid}

    # ── 첫 실행 / 상태 손상 → 무음 시딩
    if is_fresh:
        if not current_ids:
            log.warning("초기 시딩인데 수집 결과가 0건입니다(일시적 API 이상 의심). "
                        "상태파일을 만들지 않고 이번 실행을 건너뜁니다. 다음 실행에서 재시도합니다.")
            return
        if not dry_run:
            save_seen(current_ids)
        log.info("초기 실행(무음 시딩): 기존 %d건을 알림 없이 등록했습니다. "
                 "다음 실행부터 '새 공고'만 알립니다.", len(current_ids))
        if cfg.get("send_summary_on_first_run", True) and not dry_run:
            msg = (f"✅ 캐치 채용 알림이 정상 연결되었습니다.\n"
                   f"현재 조건에 맞는 공고 {len(current_ids)}건을 기준으로 등록했어요.\n"
                   f"앞으로 <b>새로 올라온 공고</b>만 알려드릴게요.\n"
                   f"(등록 시각: {scraped_ts})")
            notify_recipients(cfg["bot_token"], cfg["chat_ids"], msg, cfg)
        if not dry_run:
            note_activity(state)
        return

    # ── 신규 공고 추출
    new_items = [it for it in valid if item_id(it) not in seen]
    log.info("신규 후보: %d건", len(new_items))
    if not new_items:
        log.info("새 공고 없음.")
        if not dry_run:
            maybe_heartbeat(cfg, state, len(items), 0)
        return

    # 오래된 것부터(작은 RecruitID) 전송
    new_items.sort(key=sort_key_oldest_first)

    # ── 폭주 방지: 많으면 '폐기'가 아니라 '이번엔 일부만, 나머지는 다음 실행으로'
    cap = cfg["max_notify_per_run"]
    to_send = new_items[:cap]
    deferred = new_items[cap:]
    if deferred:
        log.warning("신규 %d건 중 %d건만 이번에 전송하고 %d건은 다음 실행으로 미룹니다(폭주 방지).",
                    len(new_items), len(to_send), len(deferred))

    newly_done = set()
    for it in to_send:
        rid = item_id(it)
        text = build_message(it, cfg, scraped_ts)
        if dry_run:
            log.info("[DRY-RUN] 전송 예정 RecruitID=%s\n%s\n%s\n%s",
                     rid, "-" * 40, text, "-" * 40)
            newly_done.add(rid)
            continue
        if notify_recipients(cfg["bot_token"], cfg["chat_ids"], text, cfg):
            newly_done.add(rid)
            record_sent(state, it.get("CompName"))   # 일일 리포트용 기록
            _extra = ", ".join(str(it.get(k)) for k in
                               ("WorkArea", "Depth", "PopularCategory",
                                "CareerGubunCode", "GubunCode", "RecruitCategory")
                               if it.get(k))
            log_to_sheet({
                "bot": "캐치",
                "scraped_at": scraped_ts,
                "company": it.get("CompName") or "",
                "title": it.get("RecruitTitle") or "",
                "link": DETAIL_URL_FMT.format(rid=rid),
                "deadline": format_deadline(it, cfg["weekday_full"]) or (it.get("ApplyEndCode") or ""),
                "extra": _extra,
            })
            log.info("전송 완료 RecruitID=%s (%s)", rid, it.get("CompName"))
        else:
            log.warning("전송 실패(일시적) RecruitID=%s → 다음 실행 때 재시도", rid)
        time.sleep(0.7)  # 공고 간 간격

    if not dry_run and newly_done:
        if save_seen(seen | newly_done):
            log.info("상태 저장: 이번에 %d건 추가 (누적 %d건)",
                     len(newly_done), len(seen | newly_done))
        note_activity(state)   # 새 공고 전송 = 활동 → 하트비트 타이머 리셋
        # 미뤄둔 공고가 있으면 안내 1건
        if deferred:
            notice = (f"ℹ️ 조건에 맞는 새 공고가 많아 {len(newly_done)}건을 먼저 보냈어요. "
                      f"남은 {len(deferred)}건은 다음 정각에 이어서 알려드릴게요.")
            notify_recipients(cfg["bot_token"], cfg["chat_ids"], notice, cfg)


def send_test(cfg):
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    msg = (f"🔔 <b>캐치 알림 테스트</b>\n"
           f"이 메시지가 보이면 봇 토큰과 chat_id 설정이 정상입니다.\n"
           f"전송 시각: {ts}")
    for cid in cfg["chat_ids"]:
        r = tg_send(cfg["bot_token"], cid, msg, cfg)
        log.info("테스트 전송 chat_id=%s → %s", cid, r)


def resend_latest(cfg, n):
    """디버깅용: 상태와 무관하게 최신 n건(RecruitID 내림차순)을 강제 전송."""
    try:
        items, _ = fetch_all(cfg)
    except Exception as e:
        log.error("공고 수집 실패로 재전송을 건너뜁니다: %s", e)
        return
    valid = [it for it in items if it.get("RecruitID") is not None]
    valid.sort(key=sort_key_oldest_first, reverse=True)  # 최신 우선
    scraped_ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    for it in valid[:n]:
        text = build_message(it, cfg, scraped_ts)
        notify_recipients(cfg["bot_token"], cfg["chat_ids"], text, cfg)
        time.sleep(0.7)
    log.info("강제 재전송 완료: %d건", min(n, len(valid)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="설정 확인용 테스트 메시지 전송")
    ap.add_argument("--dry-run", action="store_true", help="전송/저장 없이 미리보기")
    ap.add_argument("--resend-latest", type=int, metavar="N", default=0,
                    help="최근 N건 강제 재전송(디버깅)")
    args = ap.parse_args()

    log.info("=" * 50)
    log.info("캐치 알림 실행 시작")

    if not acquire_single_instance_lock():
        log.warning("다른 인스턴스가 이미 실행 중입니다. 이번 실행을 건너뜁니다.")
        return

    cfg = load_config()
    state = load_bot_state()

    try:
        if args.test:
            send_test(cfg)
        elif args.resend_latest > 0:
            resend_latest(cfg, args.resend_latest)
        else:
            if not args.dry_run:
                # 수집 실패와 무관하게 일일 리포트는 먼저 처리(놓치지 않도록)
                try:
                    maybe_daily_report(cfg, state)
                except Exception as e:
                    log.error("일일 리포트 처리 실패: %s", e)
                prune_sent_log(state)
            run(cfg, state, dry_run=args.dry_run)
    except TelegramAuthError as e:
        # 토큰이 틀려 텔레그램 노티도 불가 → red X 로 표시
        log.error("봇 토큰이 잘못되었습니다(HTTP 401): %s. "
                  "Secret(BOT_TOKEN) 을 BotFather 토큰으로 다시 확인하세요.", e)
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        # 오류 원문을 텔레그램으로 노티(같은 오류는 12h 1회). Actions 이메일 스팸 방지 위해 exit 0(green).
        log.exception("예상치 못한 오류: %s", e)
        try:
            notify_error(cfg, state, traceback.format_exc())
        except Exception as ne:
            log.error("오류 노티 전송 실패: %s", ne)
    finally:
        if not args.dry_run:
            save_bot_state(state)
        log.info("캐치 알림 실행 종료")


if __name__ == "__main__":
    main()

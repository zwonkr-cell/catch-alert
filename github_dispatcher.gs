/**
 * 채용 알림 봇 3종(인크루트·잡코리아·캐치) 외부 트리거 — GitHub Actions 를 정해진 간격으로 직접 실행
 * ─────────────────────────────────────────────────────────────────────────────
 * [왜 필요한가]
 *   GitHub 의 schedule(cron)은 '최선 노력' 방식이라 혼잡하면 실행이 지연·누락됩니다.
 *   실측(2026-10-22~11-04): 하루 예약 35회 중 약 6회만 실행, 주간 실행 간격 중앙값 약 5시간
 *   → 새 공고가 올라와도 알림이 평균 2~3시간, 길게는 8시간 늦게 왔습니다.
 *   반면 API(workflow_dispatch)로 실행을 요청하면 수 초 안에 시작됩니다.
 *   이 스크립트는 5분마다 깨어나 시간대별 간격(기본: 주간 10분·야간 60분)에 맞춰 3개 봇을 실행시킵니다.
 *
 * [설치] 약 5분 — 자세한 설명은 catch-alert 리포의 TRIGGER_SETUP.md
 *   1) GitHub 토큰: github.com → 오른쪽 위 프로필 → Settings → Developer settings
 *        → Personal access tokens → Fine-grained tokens → Generate new token
 *        · Repository access: Only select repositories → incruit-bot, job-bot, catch-alert
 *        · Permissions → Repository permissions → Actions: Read and write  (다른 권한은 건드리지 않음)
 *        · Expiration: 가능한 한 길게. 만료되면 이 스크립트가 텔레그램으로 알려 줍니다(2의 TG_* 설정 시).
 *   2) Apps Script 편집기 → 왼쪽 톱니바퀴(프로젝트 설정) → 맨 아래 '스크립트 속성' → 속성 추가
 *        · GH_TOKEN   = 1에서 만든 토큰
 *        · TG_TOKEN   = (선택) 텔레그램 봇 토큰 — 트리거 고장 알림용
 *        · TG_CHAT_ID = (선택) 알림 받을 chat_id (여러 명이면 콤마로)
 *   3) 위쪽 함수 선택에서 testDispatch → ▶실행 (처음엔 권한 승인 창 → 허용)
 *        실행 로그에 3개 모두 "OK" 가 보이면 성공(GitHub → Actions 탭에 실행이 바로 생김)
 *   4) createDispatchTrigger → ▶실행 (딱 1회) → 이후 자동
 *   · 멈추기: removeDispatchTrigger  · 상태 보기: showDispatchStatus
 *
 * 시트 기록 스크립트(google_sheets_logger.gs)와 같은 프로젝트에 '새 파일'로 넣어도 되고
 * (이름이 겹치지 않게 모두 dispatch 접두어를 씀), 별도 프로젝트로 만들어도 됩니다.
 */

var DISPATCH_OWNER = 'zwonkr-cell';
var DISPATCH_REF = 'main';
var DISPATCH_TARGETS = [
  { repo: 'incruit-bot', workflow: 'run.yml', name: '인크루트봇' },
  { repo: 'job-bot', workflow: 'run.yml', name: '잡코리아봇' },
  { repo: 'catch-alert', workflow: 'catch-alert.yml', name: '캐치봇' }
];

// KST 시간대별 실행 간격(분). 위에서부터 처음 맞는 구간을 씁니다(from 이상 ~ to 미만, 시 단위).
var DISPATCH_WINDOWS = [
  { from: 6, to: 21, every: 10 },   // 06:00~20:59 → 10분마다 (예전 GitHub 예약: 30분)
  { from: 21, to: 24, every: 60 },  // 21:00~23:59 → 1시간마다 (예전: 2시간)
  { from: 0, to: 6, every: 60 }     // 00:00~05:59 → 1시간마다 (예전: 2시간)
];
var DISPATCH_TICK_MINUTES = 5;      // 트리거가 깨어나는 주기(1, 5, 10, 15, 30 중 하나)
var DISPATCH_ALERT_HOURS = 6;       // 같은 고장 알림은 이 시간에 한 번만

/** 시간 트리거가 주기적으로 호출: 지금 시간대의 간격이 지났으면 3개 봇 실행을 요청 */
function dispatchTick() {
  var props = PropertiesService.getScriptProperties();
  var now = new Date();
  var hour = Number(Utilities.formatDate(now, 'Asia/Seoul', 'H'));
  var every = dispatchIntervalFor_(hour);
  var last = Number(props.getProperty('DISPATCH_LAST_AT') || 0);
  // 트리거 실행 시각이 1~2분 흔들려도 간격이 밀리지 않도록 1분 여유
  if (last && now.getTime() - last < (every - 1) * 60 * 1000) return;
  props.setProperty('DISPATCH_LAST_AT', String(now.getTime()));
  dispatchHandleResults_(dispatchAll_());
}

function dispatchIntervalFor_(hour) {
  for (var i = 0; i < DISPATCH_WINDOWS.length; i++) {
    var w = DISPATCH_WINDOWS[i];
    if (hour >= w.from && hour < w.to) return w.every;
  }
  return 60;
}

/** 3개 워크플로에 동시에 실행 요청(workflow_dispatch). 반환: [{target, code, body}] */
function dispatchAll_(targets) {
  targets = targets || DISPATCH_TARGETS;
  var token = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  if (!token) throw new Error('스크립트 속성 GH_TOKEN 이 없습니다(설치 2단계를 확인하세요).');
  var reqs = targets.map(function (t) {
    return {
      url: 'https://api.github.com/repos/' + DISPATCH_OWNER + '/' + t.repo +
           '/actions/workflows/' + t.workflow + '/dispatches',
      method: 'post',
      contentType: 'application/json',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
      },
      payload: JSON.stringify({ ref: DISPATCH_REF }),
      muteHttpExceptions: true
    };
  });
  var resps;
  try {
    resps = UrlFetchApp.fetchAll(reqs);
  } catch (e) {   // 네트워크 오류 등 → 다음 주기에 자동 재시도
    return targets.map(function (t) { return { target: t, code: 0, body: String(e) }; });
  }
  return resps.map(function (r, i) {
    return { target: targets[i], code: r.getResponseCode(), body: r.getContentText().slice(0, 300) };
  });
}

function dispatchOk_(r) { return r.code >= 200 && r.code < 300; }

function dispatchHandleResults_(results) {
  results.forEach(function (r) {
    console.log(r.target.name + ' → ' + (dispatchOk_(r) ? 'OK(' + r.code + ')' : '실패(' + r.code + ') ' + r.body));
  });
  // 권한/설정 문제(401·403·404·422)는 사람이 고쳐야 하므로 텔레그램으로 알림(6시간에 1번).
  // 5xx·네트워크 오류는 일시적이라 다음 주기에 자동 재시도.
  var needsHuman = results.filter(function (r) { return [401, 403, 404, 422].indexOf(r.code) >= 0; });
  if (!needsHuman.length) return;
  var props = PropertiesService.getScriptProperties();
  var lastAlert = Number(props.getProperty('DISPATCH_ALERT_AT') || 0);
  if (Date.now() - lastAlert < DISPATCH_ALERT_HOURS * 3600 * 1000) return;
  var lines = needsHuman.map(function (r) {
    return '· ' + r.target.name + ': HTTP ' + r.code + ' ' + r.body.slice(0, 120);
  });
  var hint = needsHuman.some(function (r) { return r.code === 401; })
    ? 'GitHub 토큰(GH_TOKEN)이 만료됐거나 잘못됐어요. 새 토큰을 만들어 스크립트 속성 GH_TOKEN 을 바꿔 주세요.'
    : '토큰 권한(Actions: Read and write)·대상 리포 선택·워크플로 파일 이름을 확인해 주세요.';
  var msg = '⚠️ [봇 외부 트리거] GitHub 실행 요청이 실패했어요\n\n' + lines.join('\n') +
            '\n\n■ 조치\n' + hint +
            '\n(고치기 전까지는 GitHub 자체 예약으로만 가끔 실행돼 알림이 늦어질 수 있어요)';
  if (dispatchSendTelegram_(msg)) props.setProperty('DISPATCH_ALERT_AT', String(Date.now()));
}

function dispatchSendTelegram_(text) {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty('TG_TOKEN');
  var ids = props.getProperty('TG_CHAT_ID');
  if (!token || !ids) {
    console.log('TG_TOKEN/TG_CHAT_ID 미설정 → 텔레그램 알림 생략');
    return false;
  }
  var ok = false;
  String(ids).split(/[,\s]+/).filter(String).forEach(function (cid) {
    try {
      var r = UrlFetchApp.fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
        method: 'post',
        payload: { chat_id: cid, text: text, disable_web_page_preview: 'true' },
        muteHttpExceptions: true
      });
      ok = ok || r.getResponseCode() === 200;
    } catch (e) {
      console.log('텔레그램 전송 실패: ' + e);
    }
  });
  return ok;
}

/** 설치 확인용: 지금 바로 3개 봇 실행을 요청하고 결과를 로그로 보여 줍니다(실제 실행이 1회씩 생김). */
function testDispatch() {
  dispatchAll_().forEach(function (r) {
    console.log(r.target.name + ' → ' + (dispatchOk_(r) ? 'OK' : ('실패 HTTP ' + r.code + ' ' + r.body)));
  });
}

/** 1회 실행: 주기 트리거 생성(기존 dispatchTick 트리거는 지우고 새로 만듦) + 지금 1회 실행 */
function createDispatchTrigger() {
  removeDispatchTrigger();
  ScriptApp.newTrigger('dispatchTick').timeBased().everyMinutes(DISPATCH_TICK_MINUTES).create();
  console.log('dispatchTick 트리거 생성: ' + DISPATCH_TICK_MINUTES + '분마다 깨어나 시간대별 간격에 맞춰 실행');
  PropertiesService.getScriptProperties().deleteProperty('DISPATCH_LAST_AT');
  dispatchTick();
}

/** 외부 트리거 중지(이 파일이 만든 트리거만 지움. 시트 갱신 트리거 등은 그대로) */
function removeDispatchTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'dispatchTick') ScriptApp.deleteTrigger(t);
  });
}

function showDispatchStatus() {
  var p = PropertiesService.getScriptProperties();
  var last = Number(p.getProperty('DISPATCH_LAST_AT') || 0);
  var n = ScriptApp.getProjectTriggers().filter(function (t) {
    return t.getHandlerFunction() === 'dispatchTick';
  }).length;
  console.log('트리거 ' + n + '개 · 마지막 실행 요청: ' +
              (last ? Utilities.formatDate(new Date(last), 'Asia/Seoul', 'yyyy-MM-dd HH:mm:ss') : '없음') +
              ' · GH_TOKEN ' + (p.getProperty('GH_TOKEN') ? '설정됨' : '없음') +
              ' · 텔레그램 고장 알림 ' + (p.getProperty('TG_TOKEN') && p.getProperty('TG_CHAT_ID') ? '켜짐' : '꺼짐'));
}

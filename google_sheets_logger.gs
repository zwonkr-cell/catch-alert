/**
 * 3개 봇(catch·jobkorea·incruit) 공고 자동 기록 + 유효여부/D-day 자동 갱신
 * - 봇이 새 공고 1건마다 이 웹앱으로 JSON POST → 해당 '월(YYYY-MM)' 탭에 한 줄
 * - 유효여부(Y/N): 마감 지나면 N + 행 회색음영, 남으면 Y
 * - D-day: 마감까지 남은 일수(D-3 / D-DAY / D+2). 상시·수시·채용시는 빈칸(null)
 * - 수시/채용시/상시 = 공고일로부터 15일 유효 후 자동 N (모두 KST 기준)
 * - refreshValidity() 를 시간 트리거로 자동 재계산(하루가 지나면 자동 갱신)
 *
 * [설치] 1) 확장 프로그램 → Apps Script → 이 코드 전체 붙여넣기 → 저장
 *        2) 배포 → 새 배포 → 웹 앱(실행:나 / 액세스:모든 사용자) → URL → GitHub Secret SHEET_WEBHOOK_URL
 *        3) 함수 createTriggers 선택 → ▶실행 (1회) → 1시간마다 자동 갱신
 * [이미 데이터가 있으면] 함수 migrateAddDday 를 1회 실행 → 기존 탭에 D-day 열을 안전하게 삽입
 */

var HEADERS = ['연번', '유효여부', '봇 유형', '공고시점(스크래핑)', '회사명', '지역', '공고명', '마감일자', 'D-day', '기타'];
var COL = { valid: 2, scraped: 4, deadline: 8, dday: 9 }; // 1-based 열 위치
var GRAY = '#d9d9d9';
var ROLLING = ['수시', '채용시', '상시', '충원시', '채용마감시'];

/* ───────────── 수신(기록) ───────────── */
function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var data = JSON.parse(e.postData.contents);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = getMonthSheet_(ss, data.scraped_at);

    var valid = isValid_(data.deadline, data.scraped_at) ? 'Y' : 'N';
    var dday = computeDday_(data.deadline, data.scraped_at);
    var seq = sheet.getLastRow(); // 헤더=1행 → 첫 데이터 1, 이후 순차
    sheet.appendRow([
      seq, valid, data.bot || '', data.scraped_at || '', data.company || '',
      data.region || '', hyperlink_(data.link, data.title), data.deadline || '', dday, data.extra || ''
    ]);
    if (valid === 'N') shadeRow_(sheet, sheet.getLastRow(), true);
    return json_({ ok: true, tab: sheet.getName(), seq: seq, valid: valid, dday: dday });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

/* 대시보드 백엔드: GET ?action=data | setPin | setMemo (트래픽 적어 GET 기반) */
function doGet(e) {
  var p = (e && e.parameter) || {};
  var a = p.action || '';
  if (a === 'data') return json_(getAllData_());
  if (a === 'setPin') { setMeta_(p.key, 'pinned', p.pinned === '1' || p.pinned === 'true'); return json_({ ok: true }); }
  if (a === 'setMemo') { setMeta_(p.key, 'memo', p.memo || ''); return json_({ ok: true }); }
  return ContentService.createTextOutput('채용 공고 기록 웹앱이 정상 작동 중입니다.');
}

/** 모든 월 탭의 공고 + 핀/메모(_meta) 를 합쳐 JSON 배열로 반환 */
function getAllData_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var meta = readMeta_();
  var out = [];
  var sheets = ss.getSheets();
  for (var s = 0; s < sheets.length; s++) {
    var sh = sheets[s], name = sh.getName();
    if (!/^\d{4}-\d{2}$/.test(name)) continue;
    var last = sh.getLastRow();
    if (last < 2) continue;
    var n = last - 1;
    var vals = sh.getRange(2, 1, n, HEADERS.length).getValues();
    var forms = sh.getRange(2, 7, n, 1).getFormulas(); // 공고명(7) 하이퍼링크 → URL 추출
    for (var i = 0; i < n; i++) {
      var link = extractUrl_(forms[i][0]) || '';
      var m = meta[link] || {};
      out.push({
        month: name, seq: vals[i][0], valid: String(vals[i][1] || ''), bot: String(vals[i][2] || ''),
        scraped: String(vals[i][3] || ''), company: String(vals[i][4] || ''), region: String(vals[i][5] || ''),
        title: String(vals[i][6] || ''), link: link, deadline: String(vals[i][7] || ''),
        dday: String(vals[i][8] || ''), extra: String(vals[i][9] || ''),
        pinned: !!m.pinned, memo: m.memo || ''
      });
    }
  }
  return out;
}

function extractUrl_(formula) {
  var m = String(formula || '').match(/HYPERLINK\("((?:[^"]|"")*)"/i);
  return m ? m[1].replace(/""/g, '"') : '';
}

/* ── 핀/메모 저장소(_meta 숨김 시트): key=공고링크 ── */
function metaSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName('_meta');
  if (!sh) {
    sh = ss.insertSheet('_meta');
    sh.appendRow(['key', 'pinned', 'memo', 'updated_at']);
    sh.hideSheet();
  }
  return sh;
}
function readMeta_() {
  var sh = metaSheet_();
  var last = sh.getLastRow();
  var map = {};
  if (last < 2) return map;
  var vals = sh.getRange(2, 1, last - 1, 3).getValues();
  for (var i = 0; i < vals.length; i++) {
    var key = String(vals[i][0]);
    if (!key) continue;
    map[key] = { pinned: vals[i][1] === true || vals[i][1] === 'TRUE', memo: String(vals[i][2] || '') };
  }
  return map;
}
function setMeta_(key, field, value) {
  key = String(key || '');
  if (!key) return;
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var sh = metaSheet_();
    var last = sh.getLastRow();
    var row = -1;
    if (last >= 2) {
      var keys = sh.getRange(2, 1, last - 1, 1).getValues();
      for (var i = 0; i < keys.length; i++) { if (String(keys[i][0]) === key) { row = i + 2; break; } }
    }
    if (row === -1) { sh.appendRow([key, false, '', '']); row = sh.getLastRow(); }
    if (field === 'pinned') sh.getRange(row, 2).setValue(value === true);
    if (field === 'memo') sh.getRange(row, 3).setValue(String(value || ''));
    sh.getRange(row, 4).setValue(Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd HH:mm:ss'));
  } finally {
    lock.releaseLock();
  }
}

/* ───────────── 유효여부 + D-day 자동 갱신(시간 트리거) ───────────── */
function refreshValidity() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheets = ss.getSheets();
  for (var s = 0; s < sheets.length; s++) {
    var sh = sheets[s];
    if (!/^\d{4}-\d{2}$/.test(sh.getName())) continue; // 월 탭만
    var last = sh.getLastRow();
    if (last < 2) continue;
    var n = last - 1;
    var rows = sh.getRange(2, 1, n, HEADERS.length).getValues();
    var validCol = [], ddayCol = [], bgRows = [];
    for (var i = 0; i < n; i++) {
      var scraped = rows[i][COL.scraped - 1];
      var deadline = rows[i][COL.deadline - 1];
      var v = isValid_(deadline, scraped) ? 'Y' : 'N';
      validCol.push([v]);
      ddayCol.push([computeDday_(deadline, scraped)]);
      var bg = (v === 'N') ? GRAY : null;
      var rowBg = [];
      for (var k = 0; k < HEADERS.length; k++) rowBg.push(bg);
      bgRows.push(rowBg);
    }
    sh.getRange(2, COL.valid, n, 1).setValues(validCol);         // 유효여부 열
    sh.getRange(2, COL.dday, n, 1).setValues(ddayCol);           // D-day 열
    sh.getRange(2, 1, n, HEADERS.length).setBackgrounds(bgRows); // 행 음영
  }
}

/** 1회 실행: 1시간마다 refreshValidity 자동 실행 트리거 생성 */
function createTriggers() {
  var t = ScriptApp.getProjectTriggers();
  for (var i = 0; i < t.length; i++) {
    if (t[i].getHandlerFunction() === 'refreshValidity') ScriptApp.deleteTrigger(t[i]);
  }
  ScriptApp.newTrigger('refreshValidity').timeBased().everyHours(1).create();
  refreshValidity(); // 지금 즉시 1회 갱신
}

/** 이미 데이터가 있는 기존 탭에 D-day 열을 안전 삽입(1회 실행). 데이터 보존됨. */
function migrateAddDday() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheets = ss.getSheets();
  for (var s = 0; s < sheets.length; s++) {
    var sh = sheets[s];
    if (!/^\d{4}-\d{2}$/.test(sh.getName())) continue;
    if (sh.getRange(1, COL.dday).getValue() === 'D-day') continue; // 이미 반영됨
    sh.insertColumnAfter(COL.deadline);                 // 마감일자(8) 뒤에 새 열 → 기타는 9→10 이동
    sh.getRange(1, COL.dday).setValue('D-day').setFontWeight('bold');
    sh.setColumnWidth(COL.dday, 72);
  }
  refreshValidity(); // D-day 값 채우기 + 유효여부 갱신
}

/* ───────────── 마감 판단(KST) ───────────── */
/** 유효하면 true. today(KST) <= 만료일(KST) 이면 유효 */
function isValid_(deadline, scraped) {
  var today = Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd');
  return today <= computeExpiry_(deadline, scraped);
}

/** D-day 문자열. 상시/수시/채용시/빈값 → '' (빈칸) */
function computeDday_(deadline, scraped) {
  var exp = datedExpiry_(deadline, scraped);
  if (!exp) return ''; // 실제 마감일 없음(상시류) → null 처리
  var today = Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd');
  var diff = dayDiff_(today, exp);
  if (diff > 0) return 'D-' + diff;
  if (diff === 0) return 'D-DAY';
  return 'D+' + (-diff);
}

/** 유효여부용 만료일: 실제 마감일이 있으면 그날, 상시류면 공고일+15일 */
function computeExpiry_(deadline, scraped) {
  var d = datedExpiry_(deadline, scraped);
  if (d) return d;
  var pymd = toKstYmd_(scraped) || Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd');
  var p = pymd.split('-');
  return ymd_(addDays_(kstNoon_(+p[0], +p[1], +p[2]), 15));
}

/** 실제 마감'일자'가 있으면 'YYYY-MM-DD', 상시/수시/채용시/빈값이면 null */
function datedExpiry_(deadline, scraped) {
  var s = String(deadline == null ? '' : deadline);
  var low = s.replace(/\s/g, '');
  var pymd = toKstYmd_(scraped) || Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd');
  var p = pymd.split('-');
  var py = +p[0], pm = +p[1], pd = +p[2];
  var postNoon = kstNoon_(py, pm, pd);

  var full = s.match(/(\d{4})[.\-\/](\d{1,2})[.\-\/](\d{1,2})/);
  if (full) return ymd_(kstNoon_(+full[1], +full[2], +full[3]));
  var md = s.match(/(\d{1,2})[.\-\/](\d{1,2})/);
  if (md) {
    var mm = +md[1], dd = +md[2], year = py;
    if (mm < pm - 6) year = py + 1; // 연말 걸침(12월 공고 → 1월 마감)
    return ymd_(kstNoon_(year, mm, dd));
  }
  if (/오늘|금일/.test(low)) return pymd;
  if (low.indexOf('내일') >= 0) return ymd_(addDays_(postNoon, 1));
  if (/\d{1,2}시마감/.test(low)) return pymd;
  return null; // 상시류
}

/* ───────────── 유틸 ───────────── */
function dayDiff_(fromYmd, toYmd) {
  var a = fromYmd.split('-'), b = toYmd.split('-');
  var da = kstNoon_(+a[0], +a[1], +a[2]).getTime();
  var db = kstNoon_(+b[0], +b[1], +b[2]).getTime();
  return Math.round((db - da) / 86400000);
}
function toKstYmd_(v) {
  if (v instanceof Date) return Utilities.formatDate(v, 'Asia/Seoul', 'yyyy-MM-dd');
  var m = String(v || '').match(/(\d{4})-(\d{2})-(\d{2})/);
  return m ? (m[1] + '-' + m[2] + '-' + m[3]) : null;
}
function kstNoon_(y, m, d) { return new Date(Date.UTC(y, m - 1, d, 3, 0, 0)); } // 정오 KST = 03:00 UTC
function addDays_(dateObj, days) { return new Date(dateObj.getTime() + days * 86400000); }
function ymd_(dateObj) { return Utilities.formatDate(dateObj, 'Asia/Seoul', 'yyyy-MM-dd'); }

function getMonthSheet_(ss, scrapedAt) {
  var m = String(scrapedAt || '').match(/^(\d{4})-(\d{2})/);
  var name = m ? (m[1] + '-' + m[2]) : Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM');
  var sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.appendRow(HEADERS);
    sheet.setFrozenRows(1);
    sheet.getRange('1:1').setFontWeight('bold');
    sheet.getRange('D:D').setNumberFormat('@'); // 공고시점: 텍스트 고정
    sheet.getRange('H:H').setNumberFormat('@'); // 마감일자: 텍스트 고정
    sheet.setColumnWidth(2, 70);   // 유효여부
    sheet.setColumnWidth(7, 380);  // 공고명
    sheet.setColumnWidth(9, 72);   // D-day
    sheet.setColumnWidth(10, 280); // 기타
    ss.setActiveSheet(sheet);
    ss.moveActiveSheet(1);
  }
  return sheet;
}

function shadeRow_(sheet, row, gray) {
  sheet.getRange(row, 1, 1, HEADERS.length).setBackground(gray ? GRAY : null);
}

function hyperlink_(url, title) {
  url = String(url || '').replace(/"/g, '""');
  title = String(title || '').replace(/"/g, '""');
  return url ? ('=HYPERLINK("' + url + '","' + title + '")') : title;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

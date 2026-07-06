# 캐치(catch.co.kr) 채용 → 텔레그램 알림 (GitHub 24시간 자동)

조건에 맞는 새 공고가 올라오면 **매시 정각**에 확인해서 텔레그램으로 알려줍니다.
**GitHub Actions(클라우드)** 에서 돌아가므로 내 PC가 꺼져 있어도 24시간 동작합니다.
두 계정(본인 `8755814064`, 동생 `8467039744`)이 함께 받습니다.

```
CatchAlert/
 ├─ catch_alert.py                  메인 스크립트 (표준 라이브러리만 → 설치 불필요)
 ├─ config.json                     설정 (수신자 / 필터 URL / 옵션) · 토큰은 여기 넣지 않음
 ├─ seen_ids.json                   이미 알린 공고 기록 (자동 생성·자동 커밋)
 ├─ .github/workflows/catch-alert.yml   매시 정각 실행 + 상태 커밋
 ├─ .gitignore
 ├─ run_catch_alert.bat             (선택) 내 PC 작업 스케줄러용 래퍼
 └─ README.md
```

> 🔑 **토큰 원칙**: 봇 토큰은 코드/리포지토리에 **절대 넣지 않습니다.** GitHub 는 **Secret**,
> 로컬 테스트는 환경변수(`$env:BOT_TOKEN`)로만 주입합니다.

---

# 1부 · 텔레그램 봇 만들기 (BotFather)

1. 텔레그램에서 **@BotFather** 검색 → 대화 시작
2. `/newbot` 입력
3. 봇 **표시 이름** 입력 — 예: `캐치 채용 알림`
4. 봇 **아이디(username)** 입력 — 반드시 `bot`으로 끝남, 예: `jaewon_catch_alert_bot`
5. BotFather가 주는 **토큰** 복사 — `8123456789:AAH1a2B3c4D5e6F7g8H9i0J...`
   - 이 토큰은 곧 GitHub **Secret** 에 넣습니다. 메모장 등에 잠깐 붙여두세요.
   - 노출되면 BotFather에서 `/revoke` 로 재발급.

### 봇에게 "먼저 말 걸기" (가장 흔한 실수 ⚠️)
봇은 **자기에게 먼저 말을 건 사람에게만** 보낼 수 있습니다.
**본인 + 동생 계정 각각** 봇을 검색해 대화창을 열고 **`/start`** 를 누르세요.

> ⏱️ **순서**: 첫 스케줄 실행(=시딩) **전에** 두 계정 모두 `/start` 해두세요.
> chat_id 확인은 각 계정에서 **@userinfobot** 에 `/start` → 나온 숫자가
> `8755814064`(본인)/`8467039744`(동생)와 같은지 대조.

---

# 2부 · GitHub 에 올려 24시간 자동 실행 (핵심)

### 2-1. 리포지토리 만들기
1. github.com → 우측 상단 **+** → **New repository**
2. 이름 예: `catch-alert` · **Public** (job-bot/incruit-bot 과 동일) · README 등 체크 해제(빈 리포) · **Create repository**

> 토큰과 chat_id 는 코드에 없고 **Secret 으로만** 주입하므로 공개 리포여도 안전합니다.
> (공개 리포는 GitHub Actions 실행시간이 무제한이라 매시 실행에 유리)

### 2-2. 이 폴더를 리포지토리에 올리기
PowerShell에서:
```powershell
cd D:\CatchAlert
git init
git add .
git commit -m "init: catch telegram alert"
git branch -M main
git remote add origin https://github.com/<내아이디>/catch-alert.git
git push -u origin main
```
> git 이 없거나 어렵다면 **GitHub Desktop** 으로 `D:\CatchAlert` 폴더를 열어
> "Publish repository(Private 체크)" 해도 됩니다.
> `config.json` 은 토큰이 비어 있으니 올라가도 안전하고, `seen_ids.json` 은 첫 실행 때 자동 생성됩니다.

### 2-3. Secret 2개 등록 (job-bot/incruit-bot 과 동일 방식)
리포지토리 페이지에서:
**Settings → Secrets and variables → Actions → New repository secret** — 두 개 등록:
- `BOT_TOKEN` = 1부에서 받은 봇 토큰
- `CHAT_IDS`  = `8755814064,8467039744` (콤마로 여러 명)

### 2-4. 텔레그램 연결 테스트 (수동 실행)
1. 리포지토리 상단 **Actions** 탭 → 워크플로우 **Catch Job Alert** 선택
   - (처음 Actions 탭에서 "I understand… enable" 버튼이 보이면 눌러 활성화)
2. 우측 **Run workflow** → **mode: `test`** 선택 → **Run workflow**
3. 1~2분 뒤 **두 계정 모두** `🔔 캐치 알림 테스트` 가 오면 성공입니다.
   - 안 오면 → 3부 트러블슈팅(대개 `/start` 안 함 or 토큰 오타).

### 2-5. 첫 시딩 실행
1. 다시 **Run workflow** → **mode: `run`** → 실행
2. 이 실행이 현재 공고를 **알림 없이** 기준 등록(무음 시딩)하고 "연결됨" 요약 1건만 보냅니다.
3. 실행이 끝나면 리포지토리에 `seen_ids.json` 커밋이 자동으로 생깁니다(정상).

### 2-6. 이제 자동입니다
- 이후 **매시 정각**(`cron: "0 * * * *"`, UTC 정각 = KST 정각)에 자동 실행되어
  **새로 올라온 공고만** 두 계정에 알립니다.
- 매시간 `seen_ids.json` 이 자동 커밋됩니다(= 다음 실행이 상태를 이어받음). 커밋이 쌓이는 건 정상이에요.

> ℹ️ **알아둘 점(정상 동작)**
> - GitHub 스케줄은 부하에 따라 **몇 분~수십 분 지연**되거나 드물게 한 타임 건너뛸 수 있어요.
>   놓친 공고는 중복 방지 기록 덕분에 **다음 실행에서 그대로 알림**됩니다(누락 아님).
> - 스케줄 워크플로우는 **기본 브랜치(main)** 에서만 돕니다. 반드시 main 에 올리세요.

---

# 3부 · (선택) 내 PC에서 직접 테스트/실행

GitHub 없이 확인하거나 디버깅할 때:
```powershell
cd D:\CatchAlert
$env:BOT_TOKEN = "8123456789:AAH..."   # 이 창에서만 유효 (파일에 저장 안 함)
python catch_alert.py --test            # 설정 확인용 테스트 메시지
python catch_alert.py --dry-run         # 전송/저장 없이 미리보기
python catch_alert.py                    # 실제 실행(첫 실행이면 무음 시딩)
python catch_alert.py --resend-latest 3  # 최근 3건 강제 재전송(디버깅)
```

### (대안) 내 PC를 24시간 켜두고 작업 스케줄러로 돌리기
GitHub 대신 PC에서 돌리려면 — `config.json` 의 `bot_token` 에 토큰을 넣고(이 경우 리포에 올리지 말 것),
관리자 PowerShell에서:
```powershell
schtasks /Create /TN "CatchJobAlert" /TR "D:\CatchAlert\run_catch_alert.bat" /SC HOURLY /MO 1 /ST 00:00 /F
```
단, PC가 꺼져 있으면 그 시간은 건너뜁니다. **24시간 안정 동작은 GitHub 방식(2부)을 권장합니다.**

---

# 스크래핑 주기와 차단 위험 — 안심하세요

- **매시 1회 = 하루 24회.** 1회당 요청 1건입니다. 사람이 사이트를 보는 것보다 훨씬 가벼워
  **차단 위험은 사실상 없습니다.** 브라우저 헤더 + 실패 시 지수 백오프까지 넣었습니다.
- ❌ 1분·5분 같은 짧은 주기는 금지(과함). GitHub Actions 도 최소 5분 간격이며, 매시 정각이 적절합니다.

---

# 어떻게 오류/누락을 막나요 (안전장치)

- **중복 방지** — 보낸 공고는 `seen_ids.json` 기록(원자적 저장). GitHub 에선 이 파일을 커밋해 다음 실행이 이어받음.
- **누락 방지** — 일시적 네트워크/서버 오류로 전송 실패 시 seen 처리하지 않고 **다음 정각에 재시도.**
- **폭주 방지(폐기 아님)** — 새 공고가 20건 초과면 오래된 순 20건만 보내고 **나머지는 다음 실행으로 이월.**
- **첫 실행 보호** — 첫 실행 때 API 가 빈 응답이면 잘못된 기준을 저장하지 않고 다음 실행 재시도.
- **토큰 오류 즉시 중단** — 토큰이 틀리면(401) 조용히 넘어가지 않고 멈춰 데이터 손실 방지.
- **한 수신자 문제 격리** — 동생이 봇을 차단/미시작이어도 본인은 정상 수신.
- **동시 실행 방지** — 워크플로우 `concurrency` 로 겹침 방지(로컬은 잠금 파일).
- **인코딩 안전** — 한글/이모지/특수문자·BOM 처리, 제목에 `<>&` 가 있어도 메시지 안 깨짐.

---

# 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| 테스트 메시지가 **한쪽만/안 옴** | 안 온 계정이 봇에게 `/start` 안 함, 또는 chat_id 오타 (1부) |
| Actions 로그에 `봇 토큰이 없습니다` | Secret 이름이 정확히 `BOT_TOKEN` 인지, 값에 공백/줄바꿈 없는지 확인 |
| Actions 로그에 `Unauthorized`/`401` | 토큰 값이 틀림 → Secret 다시 등록 |
| 로그에 `chat not found`(400)/`bot was blocked`(403) | 해당 계정이 봇을 시작 안 했거나 차단함 → `/start`/차단해제 |
| `seen_ids.json` 커밋 실패 / `push` 오류 | 워크플로우에 `permissions: contents: write` 가 있는지(기본 포함됨), 리포 Settings→Actions→Workflow permissions 가 "Read and write" 인지 확인 |
| 스케줄이 안 돎 | ① main 브랜치에 올렸는지 ② 60일 무활동 시 스케줄 자동중단(매시 커밋되므로 보통 무관) ③ Actions 가 활성화됐는지 |
| 스케줄이 정각보다 늦음 | GitHub 부하로 인한 정상 지연. 놓친 공고는 다음 실행에 알림됨 |
| "신규 N건 중 20건만 전송…" 로그 | 정상(폭주 방지). 나머지는 다음 정각에 이어서 보냅니다 |
| 새 공고 알림이 안 옴 | 실제로 새 공고가 없을 수 있음. Actions→Run workflow→`run` 또는 로컬 `--resend-latest 3` 로 확인 |

### 처음부터 다시(초기화)
리포지토리에서 `seen_ids.json` 을 삭제(커밋)하면 다음 실행이 다시 "첫 실행(무음 시딩)"이 됩니다.

---

# 메시지 양식

```
{기업명} - {RecruitTitle}          ← 볼드

[기본정보]
• {기업명} ({WorkArea})
• {Depth}
• {PopularCategory}
• {RecruitTitle}                   ← 하이퍼링크(상세페이지)

[추가정보]
• {CareerGubunCode}
• {GubunCode}
• {RecruitCategory}

마감까지 {Dday}일 남았어요. MM/DD(요일)에 마감돼요.
본 데이터는 {스크래핑 시점}에 스크래핑 되어 사용자에게 제공돼요.
```
- `MM/DD(요일)` 의 요일은 `config.json` 의 `weekday_full` 로 `화요일`↔`화` 전환.
- 마감이 오늘이면 "오늘(마지막 날) 마감돼요!", 상시/채용시 마감이면 해당 문구로 자동 대체.
- 필터 조건을 바꾸려면 캐치에서 필터를 걸고 개발자도구 Network 의 `getRecruitList` URL 을 복사해
  `config.json` 의 `api_url` 만 교체하면 됩니다.

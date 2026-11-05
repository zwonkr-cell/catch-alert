# 외부 트리거 설정 — 알림 지연(최대 8시간) 해결

인크루트·잡코리아·캐치 봇 3개를 **10분마다 정확히** 실행시키는 설정입니다. 한 번만 해 두면 됩니다(약 5분).

## 왜 필요한가요?

GitHub Actions 의 예약 실행(`schedule`)은 "여유 있을 때 실행"하는 방식이라, 혼잡하면 늦어지거나 아예 건너뜁니다.
실제 실행 기록(3개 봇 동일)을 보면 2026-08-27 무렵부터 급격히 나빠졌습니다.

| 기간 | 하루 실행 횟수(예약 35회) | 주간 실행 간격(중앙값) | 최대 간격 |
|---|---|---|---|
| 08/15 ~ 08/26 | 약 23~26회 | 약 40~55분 | 약 2시간 |
| 10/22 ~ 11/04 | **약 6회** | **약 5시간** | **약 8~9시간** |

그래서 새 공고가 올라와도 알림이 평균 2~3시간, 길게는 8시간 늦게 왔습니다(봇 실행 자체는 15~40초로 빠름).

반면 GitHub API 로 실행을 요청(`workflow_dispatch`)하면 **몇 초 안에** 시작됩니다.
그래서 바깥의 무료 스케줄러가 10분마다 실행을 요청하도록 바꿉니다. 기존 GitHub 예약은 **안전망**으로 그대로 둡니다.

| 시간대(KST) | 실행 간격 | 하루 |
|---|---|---|
| 06:00 ~ 20:59 | 10분 (기존 30분) | 90회 |
| 21:00 ~ 05:59 | 1시간 (기존 2시간) | 9회 |

> 간격은 `github_dispatcher.gs` 맨 위 `DISPATCH_WINDOWS` 에서 바꿀 수 있어요(예: 낮 5분).

---

## 0단계 · 먼저 세 리포의 새 버전을 main 에 반영

`incruit-bot`, `job-bot`, `catch-alert` **세 리포 모두** 새 버전(이 문서가 들어 있는 버전)이 `main` 에 들어간 뒤에 아래를 진행하세요.
외부 트리거는 실행 요청에 `mode=run` 을 함께 보내는데, 예전 워크플로는 이 입력이 없어서 요청을 **422 로 거절**합니다.
덕분에 예전 코드가 10분마다 겹쳐 도는 일(중복 알림·push 실패)은 생기지 않고, 대신 2단계의 `testDispatch` 에서 422 가 보이면 "아직 반영 안 된 리포가 있다"는 뜻이에요.

---

## 1단계 · GitHub 토큰 만들기 (공통)

1. github.com → 오른쪽 위 프로필 사진 → **Settings**
2. 왼쪽 맨 아래 **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
3. 입력
   - **Token name**: `job-bots-dispatch` (아무거나)
   - **Expiration**: 가능한 한 길게 (만료되면 텔레그램으로 알려 드려요 — 아래 2-B 참고)
   - **Repository access**: **Only select repositories** → `incruit-bot`, `job-bot`, `catch-alert` 3개 선택
   - **Permissions → Repository permissions → Actions**: **Read and write**
     (다른 권한은 건드리지 마세요. 이 토큰은 '워크플로 실행 요청'만 할 수 있습니다)
4. **Generate token** → `github_pat_...` 로 시작하는 토큰 복사 (이 화면을 벗어나면 다시 볼 수 없어요)

> 토큰은 코드·채팅·리포에 붙여넣지 말고 아래 '스크립트 속성'(또는 cron-job.org 헤더)에만 넣으세요.

---

## 2단계 (추천) · 구글 Apps Script 로 실행 — 이미 쓰는 시트 기록 스크립트에 파일 하나 추가

1. 공고가 기록되는 구글 시트 → **확장 프로그램 → Apps Script**
2. 왼쪽 **파일 +** → **스크립트** → 이름 `github_dispatcher` → 이 리포의 `github_dispatcher.gs` 내용을 **전부** 붙여넣고 저장(💾)
   - 기존 `google_sheets_logger.gs` 와 이름이 겹치지 않게 만들어 두었어요. 그대로 두셔도 됩니다.
3. 왼쪽 **⚙️ 프로젝트 설정** → 맨 아래 **스크립트 속성** → **스크립트 속성 추가**
   - `GH_TOKEN` = 1단계 토큰
   - (2-B, 선택) `TG_TOKEN` = 텔레그램 봇 토큰, `TG_CHAT_ID` = `8755814064,8467039744`
     → 토큰 만료 등으로 실행 요청이 실패하면 텔레그램으로 알려 줍니다(6시간에 1번).
4. 편집기 위쪽 함수 선택에서 **`testDispatch`** → **▶ 실행**
   - 처음엔 권한 승인 창 → 계정 선택 → (확인되지 않은 앱 경고가 뜨면) 고급 → 이동 → 허용
   - 아래 **실행 로그**에 `인크루트봇 → OK`, `잡코리아봇 → OK`, `캐치봇 → OK` 3줄이 나오면 성공
   - GitHub 각 리포 **Actions** 탭에 `workflow_dispatch` 실행이 바로 생깁니다
5. 함수 선택 **`createDispatchTrigger`** → **▶ 실행** (딱 1번) → 끝! 이제 자동입니다.
   - 왼쪽 **⏰ 트리거** 메뉴에 `dispatchTick` (5분마다) 가 보이면 정상
   - 멈추기: `removeDispatchTrigger` 실행 · 상태 보기: `showDispatchStatus` 실행

**무료 한도**: 개인 구글 계정의 트리거 실행 시간은 하루 90분까지인데, 이 스크립트는 하루 약 3~5분만 씁니다.

---

## 2단계 (대안) · cron-job.org 로 실행 — 구글 스크립트가 싫을 때

[cron-job.org](https://cron-job.org) (무료) 가입 → **CREATE CRONJOB** 을 3번 만듭니다.

| 항목 | 값 |
|---|---|
| URL (인크루트) | `https://api.github.com/repos/zwonkr-cell/incruit-bot/actions/workflows/run.yml/dispatches` |
| URL (잡코리아) | `https://api.github.com/repos/zwonkr-cell/job-bot/actions/workflows/run.yml/dispatches` |
| URL (캐치) | `https://api.github.com/repos/zwonkr-cell/catch-alert/actions/workflows/catch-alert.yml/dispatches` |
| Execution schedule | Every 10 minutes (원하면 시간대 제한) |
| ADVANCED → Request method | `POST` |
| ADVANCED → Headers | `Authorization: Bearer <1단계 토큰>` · `Accept: application/vnd.github+json` · `X-GitHub-Api-Version: 2022-11-28` · `Content-Type: application/json` |
| ADVANCED → Request body | `{"ref":"main","inputs":{"mode":"run"}}` |

저장 후 **TEST RUN** → 응답 코드 **204** 면 성공입니다.

> Apps Script 와 cron-job.org 를 **동시에** 쓰지는 마세요(실행이 2배가 될 뿐, 중복 알림은 없지만 낭비예요).

---

## 확인 · 문제 해결

- **잘 되는지**: 각 리포 Actions 탭에서 실행 이벤트가 `workflow_dispatch` 로 약 10분 간격이면 정상입니다.
- **401** (Bad credentials): 토큰 만료/오타 → 1단계를 다시 하고 `GH_TOKEN` 교체.
- **403 / 404**: 토큰에 3개 리포가 선택됐는지, 권한이 `Actions: Read and write` 인지 확인.
- **422**: 그 리포의 `main` 에 새 버전이 아직 반영되지 않았거나(0단계), 워크플로 파일 이름(`run.yml`, `catch-alert.yml`)이나 브랜치(`main`)가 바뀌었는지 확인.
- 실행이 겹쳐도 괜찮습니다: 워크플로마다 `concurrency` 로 한 번에 하나만 돌고(기다리는 실행은 가장 최근 1건만 남음),
  뒤 실행은 시작할 때 앞 실행이 저장한 **최신 기록**을 받아 오기 때문에 방금 보낸 공고를 다시 보내지 않아요.
- 외부 트리거가 멈춰도 GitHub 자체 예약(안전망)이 계속 돌기 때문에 알림이 끊기진 않습니다(대신 늦어질 수 있음).
- 비용: 공개 리포의 GitHub Actions 는 무료이고, 한 번 실행에 15~40초라 부담이 거의 없습니다.
- **되돌릴 때**: 봇 코드를 예전 버전으로 되돌리기 **전에** 먼저 `removeDispatchTrigger` 를 실행(또는 cron-job.org 작업 끄기)하세요.

## 실행 모드 (각 리포 Actions → 워크플로 → Run workflow)

| mode | 하는 일 |
|---|---|
| `run` | 정상 실행 (외부 트리거가 쓰는 기본값) |
| `dry-run` | 텔레그램 전송·시트 기록·상태 저장 없이 "무엇을 보낼지"만 로그로 확인 |
| `dry-run-proxy` | (인크루트·잡코리아) 직접 연결을 건너뛰고 한국 프록시 경로만 점검 |
| `test` | (캐치) 텔레그램 테스트 메시지 전송 |

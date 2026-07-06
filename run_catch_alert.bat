@echo off
REM ─────────────────────────────────────────────────────────────
REM  Windows 작업 스케줄러가 호출하는 실행 래퍼
REM  - 작업 폴더를 스크립트 위치로 고정 (상대경로 문제 방지)
REM  - python 절대경로 사용 (스케줄러 PATH 문제 방지)
REM  - 표준출력/에러를 scheduler_stdout.log 로도 남김
REM ─────────────────────────────────────────────────────────────
cd /d "%~dp0"
"C:\Python313\python.exe" "%~dp0catch_alert.py" >> "%~dp0scheduler_stdout.log" 2>&1

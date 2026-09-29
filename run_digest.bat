@echo off
REM Runs the Canvas digest and appends anything it prints to digest.log.
REM Task Scheduler calls this file once a day.

cd /d "%USERPROFILE%\canvas-digest"
python canvas_digest.py >> digest.log 2>&1

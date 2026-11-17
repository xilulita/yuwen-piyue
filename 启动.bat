@echo off
chcp 65001 >nul
title 智批学练
cd /d "%~dp0"
echo 正在启动「智批学练」，浏览器将自动打开…
python app.py
pause
@echo off
chcp 65001 >nul
echo 正在切换到 DeepSeek V4 Pro...
copy /y "C:\Users\78590\.claude\deepseek settings.json" "C:\Users\78590\.claude\settings.json" >nul
echo 已切换，正在启动 Claude Code...
cd /d "%~dp0"
claude
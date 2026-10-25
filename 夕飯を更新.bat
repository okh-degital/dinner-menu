@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0"
if errorlevel 1 goto folder_error
if not exist ".venv\Scripts\python.exe" goto python_error
echo 夕飯メニューを更新し、GitHub Pagesにも公開します。
".venv\Scripts\python.exe" -X utf8 -B "update_dinner.py" --publish --open
set "DINNER_RESULT=%ERRORLEVEL%"
if not "%DINNER_RESULT%"=="0" echo 更新できませんでした。上のメッセージを確認してください。
goto finish
:python_error
echo このフォルダの.venvにPythonが見つかりません。
set "DINNER_RESULT=1"
goto finish
:folder_error
echo 夕飯の作業フォルダを開けませんでした。
pause
exit /b 1
:finish
popd
pause
exit /b %DINNER_RESULT%

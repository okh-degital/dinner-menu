@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "DINNER_PROJECT=F:\SSD移行データ\Documents\夕飯自動化"
pushd "%DINNER_PROJECT%"
if errorlevel 1 goto folder_error
echo 夕飯メニューを更新します。完了までこの画面を閉じないでください。
echo.
if not exist "%DINNER_PROJECT%\.venv\Scripts\python.exe" goto python_error
"%DINNER_PROJECT%\.venv\Scripts\python.exe" -X utf8 -B "%DINNER_PROJECT%\update_dinner.py" --publish --open
set "DINNER_RESULT=%ERRORLEVEL%"
echo.
if "%DINNER_RESULT%"=="0" echo 完了しました。スマホでもページを再読み込みしてください。
if "%DINNER_RESULT%"=="3" echo 送信済みです。反映に時間がかかっています。数分後にもう一度実行してください。
if "%DINNER_RESULT%"=="1" echo 更新が途中で止まりました。上に表示された理由を確認してください。
goto finish
:python_error
echo このプロジェクト用のPythonが見つかりません。.venvフォルダの確認が必要です。
set "DINNER_RESULT=1"
goto finish
:folder_error
echo Fドライブの夕飯プロジェクトを開けませんでした。ドライブの接続を確認してください。
pause
exit /b 1
:finish
popd
pause
exit /b %DINNER_RESULT%

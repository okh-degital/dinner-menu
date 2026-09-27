"""価格確認から3人分の献立HTML・GitHub Pagesの更新までをまとめて実行。"""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import webbrowser

from build_shopping import assemble, render_html, render_markdown, scale_ingredients, source_link
from food_prices import load_prices, price_settings, render_report, score_recipe
from plan_week import DAY_OFFSETS, build_plan, parse_date, record_plan, validate_history, write_preview
from recipe_details import fetch_detail
from select_dinner import classify, collect, meal_preferences, render_candidates

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_URL = "https://okh-degital.github.io/dinner-menu/"
REPOSITORY = "okh-degital/dinner-menu"
PUBLISH_FILES = ("output/index.html", "history.json")
EXTRA_CATEGORIES = ("鶏むね肉", "豚薄切り肉", "鮭", "さば", "厚揚げ")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def json_text(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


@contextmanager
def update_lock(base):
    """OSがプロセス終了時にも解除するロック。二重クリックを防ぐ。"""
    (base / "data").mkdir(exist_ok=True)
    with (base / "data/update.lock").open("a+b") as handle:
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("別の更新が実行中です。その画面が終わるまでお待ちください。") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def candidates(pool, prices, pricing, preferences, today):
    results, seen = [], set()
    for recipe in pool["recipes"]:
        if recipe["recipe_id"] in seen:
            continue
        seen.add(recipe["recipe_id"])
        status, score, reasons = classify(recipe, preferences)
        bonus, price_reasons, primary = score_recipe(recipe, prices, pricing, today)
        if status != "candidate":
            bonus, price_reasons = 0, []
        results.append(dict(recipe, status=status, base_score=score, score=score + bonus,
                            price_bonus=bonus, reasons=reasons + price_reasons, price_reasons=price_reasons,
                            preference_reasons=[r for r in reasons if r.startswith("好みに合わせて")],
                            main_ingredients=primary))
    results.sort(key=lambda r: (-r["score"], r["recipe_id"]))
    return {"source_fetched_at": pool["fetched_at"], "stage": "candidates_only", "history_applied": False,
            "recipes": results, "price_checked_on": today.isoformat(),
            "price_warnings": prices.get("warnings", []), "categories": pool.get("categories", [])}


def saved_week(base, history, start, weekdays):
    dates = {(start + timedelta(days=DAY_OFFSETS[d])).isoformat() for d in weekdays}
    recorded = {row["date"]: row for row in history if row["date"] in dates}
    if not recorded:
        return None
    path = base / "data/weekly_plan.json"
    if not path.exists():
        raise ValueError("この週の記録はありますが、保存済みの献立データがありません。復元が必要です。")
    plan = read_json(path)
    meals = plan.get("meals", [])
    if (plan.get("week_start") != start.isoformat() or plan.get("status") != "draft_ready"
            or len(recorded) != len(dates) or {m["date"] for m in meals} != dates
            or any(m["recipe"]["recipe_id"] != recorded[m["date"]]["recipe_id"] for m in meals)):
        raise ValueError("この週の履歴と保存済み献立が一致しません。既存の献立を残して停止します。")
    return deepcopy(plan)


def prepare(base, work, start, today, cached=False):
    settings = read_json(base / "settings.json")
    pricing, preferences = price_settings(settings), meal_preferences(settings)
    weekdays = settings["weekdays"]
    if (not isinstance(weekdays, list) or not weekdays or len(set(weekdays)) != len(weekdays)
            or any(day not in DAY_OFFSETS for day in weekdays) or start.weekday() != 0):
        raise ValueError("対象曜日と週の月曜日を確認してください。")
    history = read_json(base / "history.json")
    validate_history(history)
    plan = saved_week(base, history, start, weekdays)
    print(f"対象: {start}週 / 3人分", flush=True)
    print("1/4 農水省の価格を確認しています。", flush=True)
    if cached or not pricing["enabled"]:
        prices = load_prices(base)
    else:
        from fetch_food_prices import refresh_prices
        prices = refresh_prices(work)
    for warning in prices.get("warnings", []):
        print("価格情報: " + warning, flush=True)
    details_path = base / "data/recipe_details.json"
    details = read_json(details_path) if details_path.exists() else {"schema_version": 1, "recipes": []}
    lookup = {d["recipe_id"]: d for d in details["recipes"]}
    if len(lookup) != len(details["recipes"]):
        raise ValueError("分量データのIDが重複しています。")
    artifacts = {"data/food_prices.json": json_text(prices),
                 "data/food_prices.md": render_report(prices, pricing, today)}
    if plan is not None:
        print("2/4 この週は確定済みです。同じ献立を使ってHTMLを更新します。", flush=True)
        # 今の好み設定で禁止した料理が含まれる場合、黙って変更しない。
        for meal in plan["meals"]:
            recipe = meal["recipe"]
            status, _, _ = classify(recipe, preferences)
            if status != "candidate":
                raise ValueError("確定済み献立が現在の好み設定と合いません。内容の確認が必要です。")
            bonus, reasons, primary = score_recipe(recipe, prices, pricing, parse_date(meal["date"]))
            recipe.update(price_bonus=bonus, price_reasons=reasons, main_ingredients=primary)
        plan.update(price_sources=prices.get("sources", []), price_warnings=prices.get("warnings", []))
    else:
        print("2/4 楽天レシピから献立を選んでいます。", flush=True)
        pool = read_json(base / "data/dinner_pool.json") if cached else collect(base, prices, pricing, today, preferences)
        rejected, expanded = {}, False
        while True:
            report = candidates(pool, prices, pricing, preferences, today)
            for recipe in report["recipes"]:
                if recipe["recipe_id"] in rejected:
                    recipe["status"] = "hold"
                    recipe["reasons"].append("分量の確認が必要: " + rejected[recipe["recipe_id"]])
            plan = build_plan(report, history, weekdays, start, prices, pricing, preferences)
            if plan["status"] != "draft_ready":
                if not expanded and not cached:
                    print("候補が足りないため、肉・魚・豆腐のカテゴリを追加で確認します。", flush=True)
                    pool = collect(base, prices, pricing, today, preferences, EXTRA_CATEGORIES)
                    expanded = True
                    continue
                raise ValueError("条件と分量を満たす主菜が4日分そろいませんでした。現在のHTMLと履歴は残しています。")
            new_rejections = False
            for meal in plan["meals"]:
                recipe = meal["recipe"]
                key = recipe["recipe_id"]
                if key in lookup:
                    source_link(lookup[key], key)
                    scale_ingredients(lookup[key], 3)
                    continue
                try:
                    if cached:
                        raise ValueError("保存済みの分量がありません。")
                    print("分量を確認: " + recipe["title"], flush=True)
                    time.sleep(1.1)
                    lookup[key] = fetch_detail(recipe, today)
                except ValueError as error:
                    rejected[key] = str(error)
                    print("別の候補を探します: " + str(error), flush=True)
                    new_rejections = True
            if not new_rejections:
                artifacts.update({"data/dinner_pool.json": json_text(pool),
                                  "data/dinner_candidates.json": json_text(report),
                                  "data/dinner_candidates.md": render_candidates(report, prices, pricing, today)})
                break
    print("3/4 3人分の材料と買い物リストを作成しています。", flush=True)
    dates = {meal["date"] for meal in plan["meals"]}
    # 保存済みの週も現在の上限・14日ルールで再検証する。
    record_plan([h for h in history if h["date"] not in dates], plan, preferences)
    updated_history = record_plan(history, plan, preferences)
    details["recipes"] = list(lookup.values())
    shopping = assemble(plan, details)
    html = render_html(shopping, (base / "templates/weekly.html").read_text(encoding="utf-8"))
    marker = hashlib.sha256(html.encode("utf-8")).hexdigest()
    html = html.replace("</head>", f'<meta name="dinner-build" content="{marker}">\n</head>', 1)
    write_preview(work / "data/weekly_plan.md", plan)
    artifacts.update({"output/index.html": html, "output/weekly-preview.html": html,
                      "history.json": json_text(updated_history), "data/weekly_plan.json": json_text(plan),
                      "data/weekly_plan.md": (work / "data/weekly_plan.md").read_text(encoding="utf-8"),
                      "data/recipe_details.json": json_text(details),
                      "data/shopping_list.json": json_text(shopping), "data/shopping_list.md": render_markdown(shopping)})
    return artifacts, marker


def install_artifacts(base, artifacts):
    """すべて準備できてから保存。途中の書込みエラーは元の状態に戻す。"""
    backup_root = base / "data/backups"
    backup_root.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=backup_root))
    old, written = {}, []
    for name in artifacts:
        path = base / name
        old[name] = path.read_bytes() if path.exists() else None
        if old[name] is not None:
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(old[name])
    (backup / "manifest.json").write_text(json_text({n: v is not None for n, v in old.items()}), encoding="utf-8")
    try:
        for name, text in artifacts.items():
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".update-tmp")
            temp.write_text(text, encoding="utf-8")
            temp.replace(path)
            written.append(name)
    except OSError:
        for name in reversed(written):
            path = base / name
            if old[name] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(old[name])
        raise
    return backup


def git(base, *args):
    try:
        result = subprocess.run(["git", "-c", "safe.directory=" + base.as_posix(), *args],
                                cwd=base, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Gitを実行できませんでした。Gitのインストール・接続を確認してください。") from None
    if result.returncode:
        # エラー本文に認証情報やremote URLが含まれる可能性があるため表示しない。
        raise ValueError(f"Gitの{args[0]}に失敗しました。GitHubへのログイン・接続・変更の競合を確認してください。")
    return result.stdout.strip()


def publication_preflight(base):
    remote = git(base, "remote", "get-url", "--push", "origin")
    allowed = (f"https://github.com/{REPOSITORY}.git", f"https://github.com/{REPOSITORY}", f"git@github.com:{REPOSITORY}.git")
    if remote not in allowed or git(base, "remote", "get-url", "origin") not in allowed:
        raise ValueError("GitHubの送信先が夕飯ページ用リポジトリと一致しません。")
    if git(base, "branch", "--show-current") != "main":
        raise ValueError("公開はmainブランチで実行してください。")
    git(base, "fetch", "origin", "main")
    behind, _ = map(int, git(base, "rev-list", "--left-right", "--count", "origin/main...HEAD").split())
    if behind:
        raise ValueError("GitHub側に新しい変更があります。先に変更内容の取り込みが必要です。")
    for commit in git(base, "rev-list", "origin/main..HEAD").splitlines():
        files = set(git(base, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines())
        if not files or not files <= set(PUBLISH_FILES):
            raise ValueError("夕飯ページ以外の未送信コミットがあります。自動では送信しません。")


def publish(base):
    if git(base, "diff", "HEAD", "--name-only", "--", *PUBLISH_FILES):
        git(base, "add", "--", *PUBLISH_FILES)
        git(base, "commit", "--only", "-m", "Update dinner page and planned meal history", "--", *PUBLISH_FILES)
    revision = git(base, "rev-parse", "HEAD")
    git(base, "push", "origin", "HEAD:main")
    return revision


def wait_for_public_page(marker, seconds=300):
    """公開されたHTML自体の識別子で完了を判定する。push成功だけでは完了にしない。"""
    deadline, attempts = time.monotonic() + seconds, 0
    while time.monotonic() < deadline:
        try:
            request = Request(PUBLIC_URL + "?update=" + marker[:12], headers={"Cache-Control": "no-cache", "User-Agent": "DinnerMenu/1.0"})
            with urlopen(request, timeout=15) as response:
                html = response.read(3_000_000).decode("utf-8")
            if f'name="dinner-build" content="{marker}"' in html:
                return True
        except (URLError, TimeoutError, UnicodeError):
            pass
        attempts += 1
        if attempts % 3 == 1:
            print("GitHubで公開ページを更新しています。もう少しお待ちください。", flush=True)
        time.sleep(10)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="価格・献立・3人分のHTMLを一括更新")
    parser.add_argument("--publish", action="store_true", help="スマホ用GitHub Pagesも更新")
    parser.add_argument("--open", action="store_true", help="完了後にページを開く")
    parser.add_argument("--week-start", help="対象の月曜日。省略時は日本時間で次の月曜日（当日含む）")
    parser.add_argument("--cached", action="store_true", help="取得済みのデータだけでHTMLを作る")
    parser.add_argument("--check", action="store_true", help="別フォルダに試作し、HTML・履歴の本体と公開ページを変更しない")
    args = parser.parse_args(argv)
    if args.check and args.publish:
        parser.error("--checkと--publishは同時に指定できません。")
    today = datetime.now(timezone(timedelta(hours=9))).date()
    try:
        start = parse_date(args.week_start) if args.week_start else today + timedelta(days=(-today.weekday()) % 7)
        with update_lock(BASE_DIR):
            if args.publish:
                print("GitHubへの接続を確認しています。", flush=True)
                publication_preflight(BASE_DIR)
            with tempfile.TemporaryDirectory(prefix="update-work-", dir=BASE_DIR / "data") as directory:
                work = Path(directory)
                (work / "data").mkdir()
                artifacts, marker = prepare(BASE_DIR, work, start, today, args.cached)
                if args.check:
                    check = BASE_DIR / "data/update-check"
                    for name, text in artifacts.items():
                        path = check / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text, encoding="utf-8")
                    print(f"試作が完了しました。本体と公開ページは変更していません。\n{check / 'output/index.html'}")
                    return 0
                backup = install_artifacts(BASE_DIR, artifacts)
            print(f"HTMLの作成が完了しました。更新前の控え: {backup}", flush=True)
            if args.publish:
                print("4/4 スマホ用のページを公開しています。", flush=True)
                publish(BASE_DIR)
                if not wait_for_public_page(marker):
                    print("GitHubへの送信は完了しましたが、公開ページへの反映をまだ確認できません。")
                    print("数分後に同じbatを実行すると、同じ献立のまま再確認できます。")
                    print(PUBLIC_URL)
                    return 3
                destination = PUBLIC_URL + "?update=" + marker[:12]
                print("完了しました。スマホのページにも反映されています。\n" + PUBLIC_URL, flush=True)
            else:
                destination = (BASE_DIR / "output/index.html").as_uri()
                print("パソコン内のHTMLを更新しました。公開する場合は--publishを付けます。", flush=True)
            if args.open:
                webbrowser.open(destination)
            return 0
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        message = str(error) if isinstance(error, ValueError) else "設定・データの形式またはファイルの読み書きを確認してください。"
        if isinstance(error, OSError):
            name = Path(error.filename2 or error.filename or "ファイル").name
            message = f"{name}を読み書きできません（OSエラー: {getattr(error, 'winerror', None) or error.errno}）。開いているファイルやフォルダの権限を確認してください。"
        print("更新を完了できませんでした: " + message, file=sys.stderr, flush=True)
        print("同じ週の再実行で履歴は二重登録されません。画面の内容を確認してください。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

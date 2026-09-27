"""保存済みの楽天レシピ候補から、履歴を考慮した4日分の献立案を作る。"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from select_dinner import save_json

BASE_DIR = Path(__file__).resolve().parent
DAY_OFFSETS = {"月": 0, "火": 1, "水": 2, "金": 4}


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日付はYYYY-MM-DDで指定してください。")
    return date.fromisoformat(value)


def recipe_key(recipe):
    source, recipe_id = recipe.get("source"), recipe.get("recipe_id")
    if source != "rakuten_recipe" or not isinstance(recipe_id, str) or not recipe_id:
        raise ValueError("レシピの出典・IDを確認してください。")
    return source, recipe_id


def validate_history(history):
    if not isinstance(history, list):
        raise ValueError("history.jsonは配列で保存してください。")
    dates = set()
    for item in history:
        if not isinstance(item, dict):
            raise ValueError("履歴の形式が不正です。")
        day = parse_date(item.get("date"))
        recipe_key(item)
        if day in dates:
            raise ValueError("履歴に同じ日付が複数あります。")
        dates.add(day)


def food_group(recipe):
    # 分量不明のため、栄養判定ではなく料理の種類を散らすための簡易分類。
    names = " ".join(name for name in recipe["ingredient_names"]
                     if not re.search(r"だし|出汁|スープの素|ガラスープ|エキス|コンソメ", name))
    if re.search(r"豆腐|厚揚げ", names):
        return "豆腐入り"
    if re.search(r"豚|牛肉|鶏|とり肉|手羽|ひき肉|挽肉|挽き肉|ミンチ", names):
        return "肉"
    if re.search(r"卵|玉子|たまご", names):
        return "卵"
    return "魚介・その他"


def build_plan(report, history, weekdays, week_start):
    if week_start.weekday() != 0:
        raise ValueError("開始日は月曜日を指定してください。")
    if (not isinstance(weekdays, list) or not weekdays
            or any(not isinstance(day, str) or day not in DAY_OFFSETS for day in weekdays)
            or len(set(weekdays)) != len(weekdays)):
        raise ValueError("対象曜日は月・火・水・金から重複なしで指定してください。")
    validate_history(history)
    days = sorted((week_start + timedelta(days=DAY_OFFSETS[day]), day) for day in weekdays)
    if any(item["date"] in {day.isoformat() for day, _ in days} for item in history):
        raise ValueError("対象日に記録済みの献立があります。別の週を指定してください。")
    unique = {}
    for recipe in report["recipes"]:
        if recipe.get("status") != "candidate":
            continue
        key = recipe_key(recipe)
        if not isinstance(recipe.get("score"), int) or not isinstance(recipe.get("ingredient_names"), list):
            raise ValueError("候補の点数・材料を確認してください。")
        unique.setdefault(key, recipe)
    recipes = sorted(unique.values(), key=lambda r: (-r["score"], r["recipe_id"]))
    eligible, rejected = [], []
    for day, weekday in days:
        blocked = {recipe_key(item) for item in history
                   if day - timedelta(days=14) <= parse_date(item["date"]) < day}
        eligible.append([recipe for recipe in recipes if recipe_key(recipe) not in blocked])
        rejected.append({"date": day.isoformat(), "weekday": weekday,
                         "recipe_ids": [r["recipe_id"] for r in recipes if recipe_key(r) in blocked]})

    # 最大4日・現在のカテゴリ取得では最大16件。全組合せで不足と偏りを抑える。
    best, best_score = [], (-1, -1, -1)

    def search(index, selected, used):
        nonlocal best, best_score
        if index == len(days):
            chosen = [recipe for recipe in selected if recipe is not None]
            score = (len(chosen), len({food_group(r) for r in chosen}), sum(r["score"] for r in chosen))
            if score > best_score:
                best, best_score = list(selected), score
            return
        for recipe in eligible[index]:
            key = recipe_key(recipe)
            if key not in used:
                search(index + 1, selected + [recipe], used | {key})
        search(index + 1, selected + [None], used)

    search(0, [], set())
    meals = []
    for (day, weekday), recipe in zip(days, best):
        meals.append({"date": day.isoformat(), "weekday": weekday, "recipe": recipe,
                      "group": food_group(recipe) if recipe else None,
                      "selection_reasons": ["当日の直前14日間に同じレシピIDの記録なし",
                                            "同じ週ではレシピIDを重複させない",
                                            "料理の種類を散らし、候補の評価点を優先"] if recipe else ["履歴除外後の候補不足"]})
    return {"schema_version": 1, "week_start": week_start.isoformat(),
            "status": "draft_ready" if all(best) else "insufficient_candidates",
            "target_servings": 3, "quantities_verified": False,
            "source_fetched_at": report["source_fetched_at"],
            "history_window_days": 14, "history_exclusions": rejected, "meals": meals}


def record_plan(history, plan):
    """保存済みの完成した献立案を予定として記録。再実行は重複追加しない。"""
    validate_history(history)
    if plan.get("status") != "draft_ready":
        raise ValueError("候補不足の献立案は記録できません。")
    start = parse_date(plan["week_start"])
    if start.weekday() != 0 or not plan.get("meals"):
        raise ValueError("献立案の開始日・内容を確認してください。")
    existing = {item["date"]: item for item in history}
    additions, seen_days, seen_recipes = [], set(), set()
    for meal in plan["meals"]:
        day = parse_date(meal["date"])
        if (meal["weekday"] not in DAY_OFFSETS
                or day != start + timedelta(days=DAY_OFFSETS[meal["weekday"]])
                or day in seen_days):
            raise ValueError("献立案の日付・曜日が不正です。")
        key = recipe_key(meal["recipe"])
        if key in seen_recipes:
            raise ValueError("献立案に同じレシピが重複しています。")
        seen_days.add(day)
        seen_recipes.add(key)
        if any(recipe_key(item) == key and day - timedelta(days=14) <= parse_date(item["date"]) < day
               for item in history):
            raise ValueError("献立案の作成後に履歴が変わりました。14日間の重複を確認してください。")
        if meal["date"] in existing:
            if recipe_key(existing[meal["date"]]) != key:
                raise ValueError("同じ日付に別の献立が記録されています。上書きしません。")
            continue
        additions.append({"date": meal["date"], "source": key[0], "recipe_id": key[1],
                          "title": meal["recipe"]["title"], "status": "planned"})
    return sorted(history + additions, key=lambda item: item["date"])


def write_preview(path, plan):
    lines = ["# 夕飯の献立案", "", f"対象週：{plan['week_start']}から", "",
             "3人分を予定していますが、楽天レシピの人数・分量・手順は未確認です。", "",
             "| 日付 | 曜日 | 候補 | 時間の目安 |", "|---|---|---|---|"]
    for meal in plan["meals"]:
        recipe = meal["recipe"]
        title = recipe["title"].replace("|", "／").replace("\n", " ") if recipe else "候補不足"
        estimate = recipe.get("time_estimate") or "不明" if recipe else "—"
        lines.append(f"| {meal['date']} | {meal['weekday']} | {title} | {estimate} |")
    lines += ["", "献立案の作成だけでは履歴や公開ページは更新しません。",
              "履歴の重複判定は楽天のレシピID単位です。別IDの似た料理までは判定しません。",
              "登録する履歴は食べた実績ではなく、確認した献立の予定です。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="過去14日の履歴から重複を避けた献立案を作成")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--week-start", help="対象週の月曜日（YYYY-MM-DD）")
    mode.add_argument("--record", action="store_true", help="保存済みの献立案を予定としてhistory.jsonへ記録")
    args = parser.parse_args()
    try:
        history_path = BASE_DIR / "history.json"
        history = json.loads(history_path.read_text(encoding="utf-8-sig"))
        plan_path = BASE_DIR / "data" / "weekly_plan.json"
        if args.record:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            updated = record_plan(history, plan)
            save_json(history_path, updated)
            print(f"確認した献立を予定として記録しました（新規{len(updated) - len(history)}件）。")
            return 0
        today = datetime.now(timezone(timedelta(hours=9))).date()
        start = parse_date(args.week_start) if args.week_start else today + timedelta(days=(-today.weekday()) % 7)
        report = json.loads((BASE_DIR / "data" / "dinner_candidates.json").read_text(encoding="utf-8"))
        settings = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8-sig"))
        plan = build_plan(report, history, settings["weekdays"], start)
        save_json(plan_path, plan)
        write_preview(BASE_DIR / "data" / "weekly_plan.md", plan)
        for meal in plan["meals"]:
            title = meal["recipe"]["title"] if meal["recipe"] else "候補不足"
            print(f"{meal['date']}（{meal['weekday']}）: {title}")
        print("献立案を保存しました。履歴・公開ページは変更していません。")
        return 0 if plan["status"] == "draft_ready" else 2
    except ValueError as error:
        print(f"献立を作成・記録できません: {error}", file=sys.stderr)
    except (OSError, KeyError, TypeError, AttributeError):
        print("候補・履歴・設定ファイルの保存場所と形式を確認してください。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

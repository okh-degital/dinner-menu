"""保存済みの楽天レシピ候補から、履歴を考慮した4日分の献立案を作る。"""

import argparse
import json
import re
import random
import unicodedata
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections import Counter

from select_dinner import dish_family, is_rice_bowl, meal_preferences, save_json
from food_prices import load_prices, price_settings, score_recipe

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
    if re.search(r"豚|牛肉|鶏|とり肉|手羽|ひき肉|合い?びき|挽肉|挽き肉|ミンチ", names):
        return "肉"
    if re.search(r"卵|玉子|たまご", names):
        return "卵"
    return "魚介・その他"


# カタカナ・半角表記をひらがなへ寄せてから魚種を判定する。
SEAFOOD = {
    "さば": r"さば|鯖", "さけ": r"さけ|鮭|さーもん",
    "ぶり": r"ぶり|鰤|はまち|わらさ|いなだ",
    "たら": r"たら|鱈", "あじ": r"あじ|鯵|鰺",
    "いわし": r"いわし|鰯", "さんま": r"さんま|秋刀魚",
    "まぐろ": r"まぐろ|鮪|つな|しーちきん", "かつお": r"かつお|鰹",
    "たい": r"真鯛|鯛|まだい|^たい(?:$|[（(]|の|切り身)", "さわら": r"さわら|鰆",
    "ほっけ": r"ほっけ", "ししゃも": r"ししゃも", "うなぎ": r"うなぎ|鰻",
    "かれい": r"かれい|鰈", "ひらめ": r"ひらめ|鮃",
    "えび": r"えび|海老|蝦", "いか": r"いか|烏賊", "たこ": r"たこ|蛸",
    "かに": r"かに|蟹", "ほたて": r"ほたて|帆立", "あさり": r"あさり|浅蜊",
    "しじみ": r"しじみ|蜆", "はまぐり": r"はまぐり|蛤", "かき": r"牡蠣|かき",
}


def seafood_species(recipe):
    names = recipe.get("ingredient_names")
    # 旧履歴には材料がないため料理名を補助的に使用する。
    names = names if names is not None else [recipe.get("title", "")]
    species = set()
    for name in names:
        name = unicodedata.normalize("NFKC", name)
        name = ''.join(chr(ord(c) - 96) if 'ァ' <= c <= 'ヶ' else c for c in name)
        name = re.sub(r"\s+", "", name)
        if re.search(r"だし|出汁|えきす|すーぷ|節|ふりかけ|そーす|醤油|しょうゆ|かにかま|かに風味", name):
            continue
        # 調味料・料理名の部分一致を魚介と誤認しない。
        name = re.sub(r"たこらいす|じゃがいも|あじつけ|味付け|酒|さけ蒸し|しいたけ|かき混ぜ", "", name)
        species.update(key for key, pattern in SEAFOOD.items() if re.search(pattern, name))
    return species


def outside_week(history, start):
    return [item for item in history
            if not start <= parse_date(item["date"]) < start + timedelta(days=7)]


def variety_limits(recipe, day, preferences):
    """週は月〜日、月は暦月。タコライスは丼物の枠にも含める。"""
    week = (day - timedelta(days=day.weekday())).isoformat()
    family = dish_family(recipe)
    limits = {("seafood", week, species): 1 for species in seafood_species(recipe)}
    if family is not None:
        limits[("dish", week, family)] = preferences["max_same_dish_per_week"]
    if is_rice_bowl(recipe):
        limits[("rice_bowl", week)] = preferences["max_rice_bowls_per_week"]
    if family == "タコライス":
        limits[("taco_rice", day.strftime("%Y-%m"))] = preferences["max_taco_rice_per_month"]
    return limits


def history_variety_counts(history, preferences):
    counts = Counter()
    for item in history:
        counts.update(variety_limits(item, parse_date(item["date"]), preferences).keys())
    return counts


def build_plan(report, history, weekdays, week_start, prices=None, pricing=None, preferences=None):
    preference_config = meal_preferences({"meal_preferences": preferences or {}})
    family_limit = preference_config["max_same_dish_per_week"]
    if week_start.weekday() != 0:
        raise ValueError("開始日は月曜日を指定してください。")
    if (not isinstance(weekdays, list) or not weekdays
            or any(not isinstance(day, str) or day not in DAY_OFFSETS for day in weekdays)
            or len(set(weekdays)) != len(weekdays)):
        raise ValueError("対象曜日は月・火・水・金から重複なしで指定してください。")
    validate_history(history)
    previous = {item["date"]: recipe_key(item) for item in history
                if week_start <= parse_date(item["date"]) < week_start + timedelta(days=7)}
    history = outside_week(history, week_start)
    initial_counts = history_variety_counts(history, preference_config)
    days = sorted((week_start + timedelta(days=DAY_OFFSETS[day]), day) for day in weekdays)
    unique = {}
    for recipe in report["recipes"]:
        if recipe.get("status") != "candidate":
            continue
        key = recipe_key(recipe)
        if not isinstance(recipe.get("score"), int) or not isinstance(recipe.get("ingredient_names"), list):
            raise ValueError("候補の点数・材料を確認してください。")
        unique.setdefault(key, recipe)
    recipes = sorted(unique.values(), key=lambda r: (-r["score"], r["recipe_id"]))
    random.shuffle(recipes)
    lottery = {recipe_key(r): random.random() for r in recipes}
    config = price_settings({"food_prices": pricing or {"enabled": False}})
    eligible, rejected = [], []
    for day, weekday in days:
        blocked = {recipe_key(item) for item in history
                   if day - timedelta(days=14) <= parse_date(item["date"]) < day}
        available = []
        for recipe in recipes:
            if recipe_key(recipe) in blocked:
                continue
            bonus, reasons, primary = score_recipe(recipe, prices or {"items": []}, config, day)
            available.append(dict(recipe, score=recipe.get("base_score", recipe["score"]) + bonus,
                                  price_bonus=bonus, price_reasons=reasons, main_ingredients=primary,
                                  dish_family=dish_family(recipe)))
        eligible.append(available)
        rejected.append({"date": day.isoformat(), "weekday": weekday,
                         "recipe_ids": [r["recipe_id"] for r in recipes if recipe_key(r) in blocked]})

    # 最大4日・基本4カテゴリ、丼物とタコライス、価格の追加2カテゴリで最大32件。
    best, best_score = [], (-1,)
    limits_by_day = [{recipe_key(r): variety_limits(r, day, preference_config)
                      for r in eligible[i]} for i, (day, _) in enumerate(days)]

    def search(index, selected, used, variety_counts):
        nonlocal best, best_score
        if index == len(days):
            chosen = [recipe for recipe in selected if recipe is not None]
            counts = Counter(key for r in chosen for key in r.get("main_ingredients", []))
            overuse = sum(max(0, count - config["same_main_ingredient_limit"]) for count in counts.values())
            changed_set = bool({recipe_key(r) for r in chosen} != set(previous.values()))
            changed_days = any(r is not None and recipe_key(r) != previous.get(days[i][0].isoformat())
                               for i, r in enumerate(selected))
            score = (len(chosen), changed_set, changed_days, len({food_group(r) for r in chosen}), -overuse, sum(r["score"] for r in chosen), sum(lottery[recipe_key(r)] for r in chosen))
            if score > best_score:
                best, best_score = list(selected), score
            return
        for recipe in eligible[index]:
            key = recipe_key(recipe)
            limits = limits_by_day[index][key]
            if key not in used and all(variety_counts[k] < limit for k, limit in limits.items()):
                counts = variety_counts.copy()
                counts.update(limits.keys())
                search(index + 1, selected + [recipe], used | {key}, counts)
        search(index + 1, selected + [None], used, variety_counts)

    search(0, [], set(), initial_counts)
    meals = []
    for (day, weekday), recipe in zip(days, best):
        meals.append({"date": day.isoformat(), "weekday": weekday, "recipe": recipe,
                      "group": food_group(recipe) if recipe else None,
                      "selection_reasons": ["当日の直前14日間に同じレシピIDの記録なし",
                                            "同じ週ではレシピIDと魚介の種類を重複させない",
                                            f"判定できた同じ種類の料理は週{family_limit}回まで",
                                            f"丼物は週{preference_config['max_rice_bowls_per_week']}回、タコライスは同じ月に{preference_config['max_taco_rice_per_month']}回まで",
                                            "料理の種類と主材料の偏りを抑え、候補の評価点を優先"]
                                            + recipe.get("preference_reasons", [])
                                            + recipe.get("price_reasons", []) if recipe else ["履歴・料理の重複条件による候補不足"]})
    return {"schema_version": 1, "week_start": week_start.isoformat(),
            "status": "draft_ready" if all(best) else "insufficient_candidates",
            "target_servings": 3, "quantities_verified": False,
            "source_fetched_at": report["source_fetched_at"],
            "history_window_days": 14, "history_exclusions": rejected, "meals": meals,
            "max_same_dish_per_week": family_limit,
            "max_rice_bowls_per_week": preference_config["max_rice_bowls_per_week"],
            "max_taco_rice_per_month": preference_config["max_taco_rice_per_month"],
            "price_sources": (prices or {}).get("sources", []),
            "price_warnings": (prices or {}).get("warnings", [])}


def record_plan(history, plan, preferences=None):
    """保存済みの完成した献立案を予定として記録。再実行は重複追加しない。"""
    validate_history(history)
    preference_config = meal_preferences({"meal_preferences": preferences if preferences is not None else
                                         {key: plan.get(key, 1) for key in ("max_same_dish_per_week", "max_rice_bowls_per_week", "max_taco_rice_per_month")}})
    if plan.get("status") != "draft_ready":
        raise ValueError("候補不足の献立案は記録できません。")
    start = parse_date(plan["week_start"])
    if start.weekday() != 0 or not plan.get("meals"):
        raise ValueError("献立案の開始日・内容を確認してください。")
    history = outside_week(history, start)
    variety_counts = history_variety_counts(history, preference_config)
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
        limits = variety_limits(meal["recipe"], day, preference_config)
        if any(variety_counts[k] >= limit for k, limit in limits.items()):
            raise ValueError("魚介の同一週重複、丼物の週の上限、タコライスの月の上限、または同じ料理の週の上限を超えています。")
        variety_counts.update(limits.keys())
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
    for meal in plan["meals"]:
        if meal["recipe"]:
            for reason in meal["recipe"].get("preference_reasons", []) + meal["recipe"].get("price_reasons", []):
                lines.append(f"\n- {meal['weekday']}：{reason}")
    lines.extend(plan.get("price_warnings", []))
    lines += ["", f"判定できた同じ種類の料理は週{plan.get('max_same_dish_per_week', 1)}回までです。",
              f"丼物は月〜日の週に{plan.get('max_rice_bowls_per_week', 1)}回、タコライスは同じ月に{plan.get('max_taco_rice_per_month', 1)}回までです。",
              "タコライスを選ぶ週は、丼物の枠も1回使います。記録済みの予定も数えています。",
              "タコライス・豚丼などは、投稿者やレシピIDが違っても同じ種類として数えます。",
              "献立案の作成だけでは履歴や公開ページは更新しません。",
              "過去14日の履歴との重複判定は楽天のレシピID単位です。",
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
        settings = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8-sig"))
        plan_path = BASE_DIR / "data" / "weekly_plan.json"
        if args.record:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            updated = record_plan(history, plan, meal_preferences(settings))
            save_json(history_path, updated)
            print(f"確認した献立を予定として記録しました（対象週を置き換え、履歴は計{len(updated)}件）。")
            return 0
        today = datetime.now(timezone(timedelta(hours=9))).date()
        start = parse_date(args.week_start) if args.week_start else today + timedelta(days=(-today.weekday()) % 7)
        report = json.loads((BASE_DIR / "data" / "dinner_candidates.json").read_text(encoding="utf-8"))
        plan = build_plan(report, history, settings["weekdays"], start,
                          load_prices(BASE_DIR), price_settings(settings), meal_preferences(settings))
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

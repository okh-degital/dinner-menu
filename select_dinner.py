"""夕飯の主菜候補を取得・分類する。献立の確定や公開は行わない。"""

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from fetch_rakuten import load_credentials, fetch_ranking, api_error_message
from food_prices import assess, cheap_categories, load_prices, price_settings, render_report, score_recipe

BASE_DIR = Path(__file__).resolve().parent
CATEGORY_ENDPOINT = "https://openapi.rakuten.co.jp/recipems/api/Recipe/CategoryList/20170426"
PROTEIN = re.compile(r"豚|牛肉|鶏|とり肉|手羽|ひき肉|合い?びき|挽肉|挽き肉|ミンチ|鮭|さけ|サケ|さば|鯖|サバ|ぶり|ブリ|鰤|魚|たら|タラ|鱈|いわし|イワシ|鰯|あじ|アジ|鯵|まぐろ|マグロ|さんま|サンマ|秋刀魚|えび|エビ|海老|いか|イカ|豆腐|厚揚げ|卵|玉子|たまご")
DESSERT = re.compile(r"パンケーキ|ホットケーキ|プリン|クッキー|マフィン|スイーツ|大学芋|チーズケーキ|蒸しパン")
TIMES = {"5分以内": 5, "約10分": 10, "約15分": 15, "約30分": 30, "約1時間": 60, "1時間以上": 61}
RICE_BOWL = re.compile(r"丼|どんぶり|タコライス|天津飯")
DISH_FAMILIES = (
    ("タコライス", r"タコライス|tacorice"),
    ("豚丼", r"豚丼|豚どんぶり"), ("牛丼", r"牛丼|牛どんぶり"),
    ("親子丼", r"親子丼"), ("玉子丼", r"玉子丼|卵丼|たまご丼"),
    ("天津飯", r"天津飯|天津丼"), ("カツ丼", r"カツ丼|かつ丼"),
    ("海鮮丼", r"海鮮丼"), ("鶏丼", r"鶏丼|とり丼"),
    ("ハンバーグ", r"ハンバーグ"), ("カレー", r"カレー"),
    ("生姜焼き", r"生姜焼き|しょうが焼き|ショウガ焼き"),
)


def dish_family(recipe):
    title = re.sub(r"\s+", "", unicodedata.normalize("NFKC", recipe.get("title", ""))).lower()
    for name, pattern in DISH_FAMILIES:
        if re.search(pattern, title):
            return name
    if "タコライス" in recipe.get("categories", []):
        return "タコライス"
    return None


def is_rice_bowl(recipe):
    title = re.sub(r"\s+", "", unicodedata.normalize("NFKC", recipe.get("title", "")))
    return bool(RICE_BOWL.search(title)) or dish_family(recipe) == "タコライス"


def meal_preferences(settings):
    config = {"exclude_salads": True, "rice_bowls": "prefer", "max_same_dish_per_week": 1,
              "max_rice_bowls_per_week": 1, "max_taco_rice_per_month": 1,
              **settings.get("meal_preferences", {})}
    if type(config["exclude_salads"]) is not bool or config["rice_bowls"] not in ("prefer", "allow", "hold"):
        raise ValueError("献立の好み設定を確認してください。")
    for key in ("max_same_dish_per_week", "max_rice_bowls_per_week", "max_taco_rice_per_month"):
        if type(config[key]) is not int or not 1 <= config[key] <= 4:
            raise ValueError("料理の回数の上限は1〜4回で指定してください。")
    return config


def preferred_categories(categories, preferences):
    if preferences["rice_bowls"] == "hold":
        return []
    chosen = []
    for name in ("丼物", "タコライス"):
        match = next((c for c in categories if c.get("categoryName") == name), None)
        if match:
            chosen.append(match)
    return chosen


def classify(recipe, preferences=None):
    preferences = meal_preferences({"meal_preferences": preferences or {}})
    title = recipe["title"]
    if DESSERT.search(title):
        return "excluded", 0, ["菓子・おやつを示す料理名"]
    if preferences["exclude_salads"] and re.search(r"サラダ(?!油)", title):
        return "excluded", 0, ["好みの設定によりサラダは夕飯の主菜候補から除外"]
    rice_bowl = is_rice_bowl(recipe)
    if rice_bowl and preferences["rice_bowls"] == "hold":
        return "hold", 0, ["設定により丼物・タコライスは保留"]
    if not rice_bowl and re.search(r"ご飯|ごはん|パスタ|もんじゃ|(?<!ス)ライス|トルティーヤ|うどん|そば|蕎麦|ラーメン|リゾット|チャーハン|炒飯|おにぎり|サンドイッチ|ピザ", title):
        return "hold", 0, ["主食を含む料理のため、主菜だけの候補とは別に確認"]
    if not rice_bowl and re.search(r"浅漬け|漬物|おひたし|お浸し|ナムル", title):
        return "hold", 0, ["副菜向きの料理名のため、主菜とは別に確認"]
    ingredients = recipe["ingredient_names"]
    matches = [name for name in ingredients if PROTEIN.search(name)
               and not re.search(r"だし|出汁|スープの素|ガラスープ|エキス|コンソメ|だれ|ダレ|の材料", name)]
    if not matches:
        return "hold", 0, ["材料名から主菜向きと判断できないため要確認"]
    if all(re.search(r"鶏皮|とり皮", name) for name in matches):
        return "hold", 0, ["主な肉材料が鶏皮のため、副菜・おつまみ向きか要確認"]
    if (all(re.search(r"卵|玉子|たまご", name) for name in matches)
            and not rice_bowl
            and not re.search(r"炒め|炒り|オムレツ|かに玉|ニラ玉|にら玉", title)):
        return "hold", 0, ["卵中心の料理で、主菜としての量・組み合わせを要確認"]
    score = 0
    reasons = ["主菜の材料候補: " + "、".join(matches)]
    minutes = TIMES.get(recipe.get("time_estimate"))
    if minutes is not None and minutes <= 30:
        score += 2
        reasons.append("調理時間の目安が30分以内")
    elif minutes is None:
        reasons.append("調理時間は不明")
    else:
        reasons.append("調理時間は30分超")
    if recipe.get("cost_estimate") in ("100円以下", "300円前後", "500円前後"):
        score += 1
        reasons.append("元レシピの費用目安が500円前後以下（3人分の費用ではない）")
    if rice_bowl and preferences["rice_bowls"] == "prefer":
        score += 2
        reasons.append("好みに合わせて丼物・タコライスを優先（2点加点）")
    if rice_bowl:
        reasons.append("ご飯を含めた3人分の材料を元レシピで確認")
    reasons.append("人数・分量・手順は元レシピで確認が必要")
    return "candidate", score, reasons


def category_rows(result):
    rows = []
    for level in ("large", "medium", "small"):
        for item in result.get(level, []):
            parsed = urlsplit(item.get("categoryUrl", ""))
            match = re.fullmatch(r"/category/(\d+(?:-\d+){0,2})/?", parsed.path)
            if parsed.hostname == "recipe.rakuten.co.jp" and match:
                rows.append(dict(item, categoryId=match.group(1), level=level))
    return rows


def collect(base_dir, prices=None, config=None, as_of=None, preferences=None, extra_names=()):
    app_id, access_key = load_credentials(base_dir)
    params = urlencode({"applicationId": app_id, "formatVersion": 2})
    request = Request(CATEGORY_ENDPOINT + "?" + params, headers={"accessKey": access_key})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise ValueError(api_error_message(error.code)) from None
    except (URLError, TimeoutError):
        raise ValueError("カテゴリ一覧に接続できませんでした。") from None
    categories = category_rows(payload.get("result", {}))
    chosen = [item for item in categories if item["level"] == "large"
              and item.get("categoryName") in ("肉", "魚", "卵料理", "大豆・豆腐")]
    if not chosen:
        raise ValueError("対象カテゴリが見つかりません。カテゴリ名の確認が必要です。")
    preferences = meal_preferences({"meal_preferences": preferences or {}})
    chosen += preferred_categories(categories, preferences)
    for name in extra_names:
        match = next((c for c in categories if c.get("categoryName") == name), None)
        if match and match["categoryId"] not in {c["categoryId"] for c in chosen}:
            chosen.append(match)
    if prices is not None:
        extra = cheap_categories(prices, config, as_of, categories)
        chosen += [item for item in extra if item["categoryId"] not in {c["categoryId"] for c in chosen}]
    recipes = {}
    for category in chosen:
        time.sleep(1.1)
        category_id = str(category["categoryId"])
        for recipe in fetch_ranking(app_id, access_key, category_id):
            key = recipe["recipe_id"]
            if key not in recipes:
                recipes[key] = dict(recipe, categories=[])
            recipes[key]["categories"].append(category["categoryName"])
    return {"fetched_at": datetime.now(timezone.utc).isoformat(),
            "categories": [{"id": str(c["categoryId"]), "name": c["categoryName"]} for c in chosen],
            "recipes": list(recipes.values())}


def save_json(path, value):
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def render_candidates(report, prices, config, as_of):
    def plain(value):
        return str(value or "不明").replace("\n", " ").replace("\r", " ")

    cheap = [item for item in prices.get("items", []) if assess(item, config, as_of) == "cheap"]
    lines = ["# 楽天レシピの夕飯候補", "", f"価格の判定日：{as_of.isoformat()}",
             f"レシピの取得日時：{report['source_fetched_at']}", ""]
    if not config["enabled"]:
        lines.append("価格による優先は無効です。通常の条件で候補を選んでいます。")
    elif cheap:
        lines.append("割安な食材：" + "、".join(plain(item["ingredient"]) for item in cheap))
    else:
        lines.append("有効期間内で割安条件に該当する食材がないため、通常の条件で候補を選んでいます。")
    lines += ["", "取得したカテゴリ：" + "、".join(plain(c["name"]) for c in report.get("categories", [])),
              "", "候補一覧には同じ種類の料理の別レシピも含みます。週の献立を作るときに回数を制限します。",
              "この段階では過去14日の履歴との照合と3人分の分量確認はまだ行っていません。", ""]
    for status, heading in (("candidate", "主菜候補"), ("hold", "保留"), ("excluded", "除外")):
        recipes = [r for r in report["recipes"] if r["status"] == status]
        lines += [f"## {heading}（{len(recipes)}件）", ""]
        for recipe in recipes:
            # APIのURLには追跡用の値を含む場合があるため、公開レシピIDからリンクを作る。
            title = plain(recipe["title"]).replace("[", "［").replace("]", "］").replace("<", "＜").replace(">", "＞")
            key = str(recipe["recipe_id"])
            lines.append(f"### [{title}](https://recipe.rakuten.co.jp/recipe/{key}/)" if key.isdigit() else f"### {title}")
            lines += ["", "主な材料の候補：" + ("、".join(plain(n) for n in recipe.get("main_ingredients", [])) or "要確認"),
                      "調理時間の目安：" + plain(recipe.get("time_estimate")), ""]
            family = dish_family(recipe)
            if family:
                lines += ["同じ料理としてまとめる種類：" + family, ""]
            lines.extend("- " + plain(reason).replace("<", "＜").replace(">", "＞") for reason in recipe["reasons"])
            lines.append("")
    lines += ["価格の出典は food_prices.md、4日分への絞り込みは plan_week.py で確認できます。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="夕飯候補を理由付きで保存")
    parser.add_argument("--refresh", action="store_true", help="農水省の相場と楽天レシピ候補を再取得")
    args = parser.parse_args()
    cache = BASE_DIR / "data" / "dinner_pool.json"
    try:
        settings = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8-sig"))
        config = price_settings(settings)
        preferences = meal_preferences(settings)
        today = datetime.now(timezone(timedelta(hours=9))).date()
        prices = load_prices(BASE_DIR)
        if args.refresh and config["enabled"]:
            from fetch_food_prices import refresh_prices
            try:
                prices = refresh_prices(BASE_DIR)
            except ValueError as error:
                prices = {"items": [], "warnings": [str(error)]}
                save_json(BASE_DIR / "data/food_prices.json", prices)
        (BASE_DIR / "data").mkdir(exist_ok=True)
        (BASE_DIR / "data/food_prices.md").write_text(render_report(prices, config, today), encoding="utf-8")
        if args.refresh:
            pool = collect(BASE_DIR, prices, config, today, preferences)
            if not pool["recipes"]:
                raise ValueError("0件のため既存の候補は更新しません。")
            save_json(cache, pool)
        else:
            if not cache.exists():
                raise ValueError("初回は python select_dinner.py --refresh を実行してください。")
            pool = json.loads(cache.read_text(encoding="utf-8"))
        results = []
        seen = set()
        for recipe in pool["recipes"]:
            if recipe["recipe_id"] in seen:
                continue
            seen.add(recipe["recipe_id"])
            status, score, reasons = classify(recipe, preferences)
            bonus, price_reasons, primary = score_recipe(recipe, prices, config, today)
            if status != "candidate":
                bonus, price_reasons = 0, []
            results.append(dict(recipe, status=status, base_score=score, price_bonus=bonus,
                                score=score + bonus, reasons=reasons + price_reasons,
                                preference_reasons=[reason for reason in reasons if reason.startswith("好みに合わせて")],
                                price_reasons=price_reasons, main_ingredients=primary))
        results.sort(key=lambda r: (-r["score"], r["recipe_id"]))
        report = {"source_fetched_at": pool["fetched_at"], "stage": "candidates_only",
                  "history_applied": False, "recipes": results,
                  "price_checked_on": today.isoformat(), "price_warnings": prices.get("warnings", []),
                  "categories": pool.get("categories", [])}
        save_json(BASE_DIR / "data" / "dinner_candidates.json", report)
        (BASE_DIR / "data/dinner_candidates.md").write_text(render_candidates(report, prices, config, today), encoding="utf-8")
        candidates = [r for r in results if r["status"] == "candidate"]
        print(f"取得済み{len(results)}件から主菜候補{len(candidates)}件。残りは保留・除外として理由を保存しました。")
        for recipe in candidates[:4]:
            print("・" + recipe["title"] + "（" + str(recipe.get("time_estimate")) + "）")
            for reason in recipe["price_reasons"]:
                print("  " + reason)
        for warning in prices.get("warnings", []):
            print(warning)
        print("候補の保存のみ。履歴除外・3人分への換算・公開はまだ行っていません。")
        print("候補のレシピリンクと選んだ理由: data/dinner_candidates.md")
        return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyError, TypeError, AttributeError):
        print("候補作成に失敗しました。認証・通信・入力データの形式を確認してください。", file=sys.stderr)
        return 1
    except OSError:
        print("ファイルを読み書きできませんでした。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

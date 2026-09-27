"""夕飯の主菜候補を取得・分類する。献立の確定や公開は行わない。"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fetch_rakuten import load_credentials, fetch_ranking

BASE_DIR = Path(__file__).resolve().parent
CATEGORY_ENDPOINT = "https://openapi.rakuten.co.jp/recipems/api/Recipe/CategoryList/20170426"
PROTEIN = re.compile(r"豚|牛肉|鶏|とり肉|手羽|ひき肉|挽肉|挽き肉|ミンチ|鮭|さけ|サケ|さば|鯖|サバ|ぶり|ブリ|鰤|魚|たら|タラ|鱈|いわし|イワシ|鰯|あじ|アジ|鯵|まぐろ|マグロ|さんま|サンマ|秋刀魚|えび|エビ|海老|いか|イカ|豆腐|厚揚げ|卵|玉子|たまご")
DESSERT = re.compile(r"パンケーキ|ホットケーキ|プリン|クッキー|マフィン|スイーツ|大学芋|チーズケーキ|蒸しパン")
TIMES = {"5分以内": 5, "約10分": 10, "約15分": 15, "約30分": 30, "約1時間": 60, "1時間以上": 61}


def classify(recipe):
    title = recipe["title"]
    if DESSERT.search(title):
        return "excluded", 0, ["菓子・おやつを示す料理名"]
    if re.search(r"ご飯|ごはん|パスタ|もんじゃ", title):
        return "hold", 0, ["主食を含む料理のため、主菜だけの候補とは別に確認"]
    ingredients = recipe["ingredient_names"]
    matches = [name for name in ingredients if PROTEIN.search(name)
               and not re.search(r"だし|出汁|スープの素|ガラスープ|エキス|コンソメ|だれ|ダレ|の材料", name)]
    if not matches:
        return "hold", 0, ["材料名から主菜向きと判断できないため要確認"]
    if all(re.search(r"鶏皮|とり皮", name) for name in matches):
        return "hold", 0, ["主な肉材料が鶏皮のため、副菜・おつまみ向きか要確認"]
    if (all(re.search(r"卵|玉子|たまご", name) for name in matches)
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
    reasons.append("人数・分量・手順は元レシピで確認が必要")
    return "candidate", score, reasons


def collect(base_dir):
    app_id, access_key = load_credentials(base_dir)
    params = urlencode({"applicationId": app_id, "formatVersion": 2, "categoryType": "large"})
    request = Request(CATEGORY_ENDPOINT + "?" + params, headers={"accessKey": access_key})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise ValueError(f"カテゴリ一覧の取得に失敗しました（HTTP {error.code}）。") from None
    except (URLError, TimeoutError):
        raise ValueError("カテゴリ一覧に接続できませんでした。") from None
    categories = payload.get("result", {}).get("large", [])
    if not isinstance(categories, list):
        raise ValueError("カテゴリ一覧の形式を確認できませんでした。")
    chosen = [item for item in categories if isinstance(item, dict)
              and item.get("categoryName") in ("肉", "魚", "卵料理", "大豆・豆腐")]
    if not chosen:
        raise ValueError("対象カテゴリが見つかりません。カテゴリ名の確認が必要です。")
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


def main():
    parser = argparse.ArgumentParser(description="夕飯候補を理由付きで保存")
    parser.add_argument("--refresh", action="store_true", help="肉・魚・卵・豆腐の候補をAPIから再取得")
    args = parser.parse_args()
    cache = BASE_DIR / "data" / "dinner_pool.json"
    try:
        if args.refresh:
            pool = collect(BASE_DIR)
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
            status, score, reasons = classify(recipe)
            results.append(dict(recipe, status=status, score=score, reasons=reasons))
        results.sort(key=lambda r: (-r["score"], r["recipe_id"]))
        report = {"source_fetched_at": pool["fetched_at"], "stage": "candidates_only",
                  "history_applied": False, "recipes": results}
        save_json(BASE_DIR / "data" / "dinner_candidates.json", report)
        candidates = [r for r in results if r["status"] == "candidate"]
        print(f"取得済み{len(results)}件から主菜候補{len(candidates)}件。残りは保留・除外として理由を保存しました。")
        for recipe in candidates[:4]:
            print("・" + recipe["title"] + "（" + str(recipe.get("time_estimate")) + "）")
        print("候補の保存のみ。履歴除外・3人分への換算・公開はまだ行っていません。")
        return 0
    except (ValueError, KeyError, TypeError, AttributeError):
        print("候補作成に失敗しました。認証・通信・入力データの形式を確認してください。", file=sys.stderr)
        return 1
    except OSError:
        print("ファイルを読み書きできませんでした。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

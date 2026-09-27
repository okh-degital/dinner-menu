"""楽天レシピのランキングを取得する。公開ページの生成とは別に実行する。"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
ENDPOINT = "https://openapi.rakuten.co.jp/recipems/api/Recipe/CategoryRanking/20170426"


def load_credentials(base_dir):
    config_path = base_dir / "rakuten.local.json"
    config = {}
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("rakuten.local.jsonはJSONオブジェクトで記入してください。")
    app_id = os.environ.get("RAKUTEN_APPLICATION_ID") or config.get("application_id", "")
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY") or config.get("access_key", "")
    if not all(isinstance(value, str) and value.strip() for value in (app_id, access_key)):
        raise ValueError("アプリID・アクセスキーが未設定です。RAKUTEN_SETUP.mdを確認してください。")
    return app_id.strip(), access_key.strip()


def normalize_recipes(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
        raise ValueError("楽天APIの応答形式が想定と異なります。保存は行いません。")
    recipes = []
    for item in payload["result"]:
        if not isinstance(item, dict):
            raise ValueError("レシピ情報の形式が不正です。")
        recipe_id = item.get("recipeId")
        title, url = item.get("recipeTitle"), item.get("recipeUrl")
        materials = item.get("recipeMaterial")
        if (not isinstance(recipe_id, (str, int)) or isinstance(recipe_id, bool)
                or not str(recipe_id).strip() or not isinstance(title, str) or not title.strip()
                or not isinstance(url, str) or not isinstance(materials, list)
                or not all(isinstance(name, str) for name in materials)):
            raise ValueError("必須のレシピ情報が不足しています。")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "recipe.rakuten.co.jp" or parsed.username:
            raise ValueError("レシピURLの形式が想定と異なります。")
        recipes.append({
            "source": "rakuten_recipe",
            "recipe_id": str(recipe_id),
            "title": title,
            "url": url,
            "ingredient_names": materials,
            "time_estimate": item.get("recipeIndication"),
            "cost_estimate": item.get("recipeCost"),
            "author": item.get("nickname"),
            "rank": item.get("rank"),
            # APIには人数・分量・手順がない。取得済みの値と混同しない。
            "servings": None,
            "ingredient_quantities": None,
            "steps": None,
            "needs_quantity_review": True,
        })
    return recipes


def fetch_ranking(app_id, access_key, category_id=None):
    params = {"applicationId": app_id, "format": "json", "formatVersion": 2}
    if category_id:
        if not re.fullmatch(r"[0-9]+(?:-[0-9]+){0,2}", category_id):
            raise ValueError("カテゴリIDは10や10-276の形式で指定してください。")
        params["categoryId"] = category_id
    request = Request(ENDPOINT + "?" + urlencode(params), headers={
        "accessKey": access_key,
        "Accept": "application/json",
        "User-Agent": "dinner-menu/0.1",
    })
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        # 応答本文やリクエストURLは認証情報を含み得るため表示しない。
        raise ValueError(f"楽天APIがHTTP {error.code}を返しました。アプリの利用許可・設定を確認してください。") from None
    except (URLError, TimeoutError):
        raise ValueError("楽天APIへ接続できませんでした。時間をおいて再実行してください。") from None
    return normalize_recipes(payload)


def main():
    parser = argparse.ArgumentParser(description="楽天レシピ候補を取得（省略時は総合ランキング）")
    parser.add_argument("--category-id", help="公式カテゴリID（例: 10）")
    args = parser.parse_args()
    try:
        app_id, access_key = load_credentials(BASE_DIR)
        recipes = fetch_ranking(app_id, access_key, args.category_id)
        if not recipes:
            raise ValueError("レシピが0件のため、既存の取得結果は更新しません。")
        output = BASE_DIR / "data" / "rakuten_recipes.json"
        output.parent.mkdir(exist_ok=True)
        result = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "category_id": args.category_id,
            "recipes": recipes,
        }
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)
    except json.JSONDecodeError:
        print("JSONを読み込めませんでした。設定ファイルまたはAPIの応答を確認してください。", file=sys.stderr)
        return 1
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("ファイルの読み書きに失敗しました。保存先の権限を確認してください。", file=sys.stderr)
        return 1
    print(f"{len(recipes)}件のレシピ候補を保存しました: {output}")
    print("人数・分量・手順は未取得です。公開ページはまだ変更していません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

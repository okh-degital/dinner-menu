"""農水省の相場を、期限と主材料を確認して献立の優先度に反映する。"""

import json
import re
import unicodedata
from datetime import date


DEFAULTS = {
    "enabled": True,
    "cheap_below_normal_percent": 10,
    "priority": "standard",
    "max_age_days": {"vegetables": 14, "meat_eggs": 45},
    "max_extra_categories": 2,
    "same_main_ingredient_limit": 2,
}
WEIGHTS = {"low": 1, "standard": 2, "high": 3}
ALIASES = {
    "キャベツ": ["キャベツ"], "ねぎ": ["長ねぎ", "長ネギ", "白ねぎ", "白ネギ", "長葱", "ねぎ", "ネギ", "葱"],
    "レタス": ["レタス"], "ばれいしょ": ["じゃがいも", "ジャガイモ", "じゃが芋", "ジャガ芋", "馬鈴薯", "ばれいしょ"],
    "たまねぎ": ["たまねぎ", "玉ねぎ", "玉葱", "タマネギ", "玉ネギ"],
    "きゅうり": ["きゅうり", "キュウリ", "胡瓜"], "トマト": ["トマト", "とまと"],
    "にんじん": ["にんじん", "ニンジン", "人参"], "はくさい": ["はくさい", "ハクサイ", "白菜"],
    "だいこん": ["だいこん", "ダイコン", "大根"],
    "輸入牛ロース": ["輸入牛ロース", "輸入牛ロース肉"],
    "国産牛ロース": ["国産牛ロース", "国産牛ロース肉"],
    "豚ロース": ["豚ロース", "豚ロース肉", "豚肉ロース"],
    "鶏もも肉": ["鶏もも肉", "鶏モモ肉", "鶏モモ", "鶏もも", "とりもも肉", "とりもも"],
    "卵": ["卵", "玉子", "たまご", "タマゴ", "鶏卵"],
    # 以下は偏りの判定専用。農水省の別規格の価格を流用しない。
    "鶏むね肉": ["鶏むね肉", "鶏胸肉", "鶏ムネ肉", "とりむね肉"],
    "鶏ひき肉": ["鶏ひき肉", "鶏挽肉", "鶏挽き肉", "鶏ミンチ"],
    "豚肉": ["豚肉", "豚こま肉", "豚こま", "豚バラ肉", "豚バラ", "豚薄切り肉"],
    "豆腐": ["豆腐", "木綿豆腐", "絹ごし豆腐", "絹豆腐"],
    "えび": ["えび", "エビ", "海老", "むきえび", "むき海老"],
    "鮭": ["鮭", "生鮭", "さけ", "サケ"], "さば": ["さば", "サバ", "鯖"],
}
CATEGORY_NAMES = {key: values for key, values in ALIASES.items()}
CATEGORY_NAMES.update({"ばれいしょ": ["じゃがいも"], "たまねぎ": ["玉ねぎ"],
                       "はくさい": ["白菜"], "だいこん": ["大根"], "ねぎ": ["長ネギ（ねぎ）"],
                       "国産牛ロース": ["牛かたまり肉・ステーキ用・焼肉用"],
                       "輸入牛ロース": ["牛かたまり肉・ステーキ用・焼肉用"],
                       "豚ロース": ["豚ロース"], "卵": ["卵料理"]})


def normalize(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def price_settings(settings):
    config = {**DEFAULTS, **settings.get("food_prices", {})}
    config["max_age_days"] = {**DEFAULTS["max_age_days"], **config["max_age_days"]}
    if type(config["enabled"]) is not bool or config["priority"] not in WEIGHTS:
        raise ValueError("価格設定の有効・無効と優先度を確認してください。")
    for key, low, high in [("cheap_below_normal_percent", 1, 50), ("max_extra_categories", 0, 2),
                           ("same_main_ingredient_limit", 1, 4)]:
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"価格設定の{key}を確認してください。")
    if any(type(v) is not int or not 1 <= v <= 60 for v in config["max_age_days"].values()):
        raise ValueError("価格情報の有効日数は1〜60日で指定してください。")
    return config


def load_prices(base_dir):
    path = base_dir / "data/food_prices.json"
    if not path.exists():
        return {"items": [], "warnings": ["相場が未取得のため、価格による優先は行いません。"]}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data.get("items"), list):
            raise ValueError
        fields = {"ingredient", "label", "price_yen", "unit", "normal_percent", "survey_date", "group", "source_url"}
        if any(not isinstance(item, dict) or not fields.issubset(item)
               or item["group"] not in DEFAULTS["max_age_days"]
               or not all(isinstance(item[key], str) for key in ("ingredient", "label", "unit", "survey_date", "source_url"))
               for item in data["items"]):
            raise ValueError
        return data
    except (ValueError, AttributeError, TypeError):
        return {"items": [], "warnings": ["相場データを読めないため、価格による優先は行いません。"]}


def assess(item, config, as_of):
    """取得日でなく調査開始日から有効期間を判定する。"""
    try:
        age = (as_of - date.fromisoformat(item["survey_date"])).days
        limit = config["max_age_days"][item["group"]]
        ratio = item["normal_percent"]
        if type(ratio) not in (int, float) or not 0 < ratio < 1000:
            return "unknown"
    except (KeyError, TypeError, ValueError):
        return "unknown"
    if age < 0 or age > limit:
        return "stale"
    return "cheap" if ratio <= 100 - config["cheap_below_normal_percent"] else "normal"


def main_ingredients(recipe):
    """分量不明のため、先頭材料か料理名にも出る食材だけを保守的に判定。"""
    title = normalize(recipe.get("title", ""))
    result = set()
    for index, raw in enumerate(recipe["ingredient_names"]):
        name = normalize(raw)
        if re.search(r"だし|出汁|スープ|エキス|コンソメ|飾り|付け合|つけ合|添え|ソース|ケチャップ|粉末", name):
            continue
        name = re.sub(r"[（(].*?[）)]", "", name).strip("★☆●○◎◆◇＊*・")
        for key, aliases in ALIASES.items():
            if name in aliases and (index == 0 or any(alias in title for alias in aliases)):
                result.add(key)
    return result


def score_recipe(recipe, prices, config, as_of):
    if not config["enabled"]:
        return 0, [], []
    primary = main_ingredients(recipe)
    matches = {item["ingredient"]: item for item in prices.get("items", [])
               if item.get("ingredient") in primary and assess(item, config, as_of) == "cheap"}
    reasons = [f"{key}が平年比{item['normal_percent']}％で割安"
               f"（全国平均・{item['survey_date']}調査）" for key, item in sorted(matches.items())]
    # 食材が多い料理ばかり選ばないよう、1品あたりの加点は一定。
    return WEIGHTS[config["priority"]] if matches else 0, reasons, sorted(primary)


def cheap_categories(prices, config, as_of, categories):
    if not config["enabled"]:
        return []
    cheap = sorted((item for item in prices.get("items", []) if assess(item, config, as_of) == "cheap"),
                   key=lambda item: (item["normal_percent"], item["ingredient"]))
    result, seen = [], set()
    for item in cheap:
        names = CATEGORY_NAMES.get(item["ingredient"], [])
        for category in categories:
            if category.get("categoryName") in names and str(category["categoryId"]) not in seen:
                seen.add(str(category["categoryId"]))
                result.append(category)
                break
        if len(result) >= config["max_extra_categories"]:
            break
    return result[:config["max_extra_categories"]]


def render_report(prices, config, as_of):
    labels = {"cheap": "優先候補", "normal": "通常", "stale": "期限外・加点なし", "unknown": "確認できず・加点なし"}
    lines = ["# 食材の相場", "", f"判定日：{as_of.isoformat()}", "",
             "農水省の全国平均小売価格です。特売価格を含みません。店頭価格や3人分の合計額ではありません。",
             f"価格による優先：{'有効' if config['enabled'] else '無効'}。"
             f"平年より{config['cheap_below_normal_percent']}％以上安い食材を優先します。",
             f"有効期間：調査開始日から野菜{config['max_age_days']['vegetables']}日、"
             f"肉・卵{config['max_age_days']['meat_eggs']}日。", "",
             "| 食材・規格 | 価格 | 平年比 | 調査開始日 | 判定 |", "|---|---|---|---|---|"]
    for item in prices.get("items", []):
        lines.append(f"| {item['label']} | {item['price_yen']}円/{item['unit']} | {item['normal_percent']}％ | "
                     f"{item['survey_date']} | {labels[assess(item, config, as_of)]} |")
    lines += ["", "出典："]
    for source in prices.get("sources", []):
        lines.append(f"- [{source['label']}]({source['url']})")
    lines += ["", *prices.get("warnings", []), ""]
    return "\n".join(lines)

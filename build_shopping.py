"""確認済みの元分量を人数換算し、買い物リスト付きの献立プレビューを生成。"""

import json
import re
import unicodedata
import sys
from collections import defaultdict
from fractions import Fraction
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
UNITS = {"g", "ml", "個", "枚", "束", "袋", "本", "丁", "大さじ", "小さじ",
         "切れ", "尾", "匹", "片", "パック", "玉", "房", "合", "カップ", "人分",
         "cm", "つ", "茶碗杯", "かけ", "株", "つかみ", "片分", "膳"}


def number(value):
    whole, rest = divmod(value.numerator, value.denominator)
    if not rest:
        return str(whole)
    fraction = f"{rest}/{value.denominator}"
    return f"{whole}と{fraction}" if whole else fraction


def amount_text(amount, unit):
    if unit == "小さじ" and amount >= 3:
        tablespoons, remainder = divmod(amount, 3)
        text = "大さじ" + number(Fraction(tablespoons))
        return text + ("＋小さじ" + number(remainder) if remainder else "")
    if unit == "茶碗杯":
        return "茶碗" + number(amount) + "杯"
    return unit + number(amount) if unit in ("大さじ", "小さじ") else number(amount) + unit


def source_link(detail, recipe_id):
    url = detail.get("source_url", "")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "recipe.rakuten.co.jp"
            or parsed.path != f"/recipe/{recipe_id}/" or parsed.query or parsed.fragment):
        raise ValueError("確認データの出典URLとレシピIDが一致しません。")
    return url


def ingredient_name(name):
    name = re.sub(r"^[★☆●○◎◆◇■□△▲・*\s]+", "", unicodedata.normalize("NFKC", name).strip())
    return {"醤油": "しょうゆ", "醬油": "しょうゆ", "しょう油": "しょうゆ", "鶏挽肉": "鶏ひき肉",
            "塩コショウ": "塩・こしょう", "塩こしょう": "塩・こしょう"}.get(name, name)


def ingredient_heading(name):
    return bool(re.fullmatch(r"【[^】]*(?:材料|調味料|たれ|タレ|あん|ソース|トッピング)[^】]*】|[●★◆].+の材料", name))


def unknown_detail(recipe):
    """APIの材料名を保持し、取得できない分量は推測しない。"""
    return {"recipe_id": recipe["recipe_id"],
            "source_url": f'https://recipe.rakuten.co.jp/recipe/{recipe["recipe_id"]}/',
            "source_servings": None, "verified": False, "verified_on": "",
            "ingredients": [{"name": ingredient_name(name), "amount": None, "unit": "", "note": "不明"}
                            for name in recipe.get("ingredient_names", [])
                            if isinstance(name, str) and ingredient_name(name) and not ingredient_heading(name)],
            "notes": ["分量は不明です。材料・人数・作り方は元レシピをご確認ください。"]}


def scale_ingredients(detail, servings):
    base = detail.get("source_servings")
    if type(servings) is not int or servings <= 0:
        raise ValueError("換算先の人数を確認してください。")
    can_scale = detail.get("verified") is True and type(base) is int and base > 0
    result = []
    for item in detail.get("ingredients", []):
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError("材料名を確認してください。")
        note, unit = item.get("note", ""), item.get("unit", "")
        if not isinstance(note, str):
            raise ValueError("材料の注記を確認してください。")
        amount = None
        if can_scale and item.get("amount") is not None and unit in UNITS:
            try:
                amount = Fraction(str(item["amount"])) * Fraction(servings, base)
            except (ValueError, ZeroDivisionError):
                pass
        if amount is None or amount <= 0:
            amount, text, unit, note = None, "不明", "", "不明"
        else:
            text = amount_text(amount, unit)
            if note:
                text += f"（{note}）"
        result.append({"name": item["name"], "amount": str(amount) if amount is not None else None,
                       "unit": unit, "note": note, "display": text})
    return result


def assemble(plan, details):
    if plan.get("status") != "draft_ready" or not plan.get("meals"):
        raise ValueError("4日分の献立案を先に作成してください。")
    servings = plan["target_servings"]
    lookup = {}
    for detail in details["recipes"]:
        if detail["recipe_id"] in lookup:
            raise ValueError("確認データのレシピIDが重複しています。")
        lookup[detail["recipe_id"]] = detail
    meals, totals = [], {}
    for meal in plan["meals"]:
        recipe = meal["recipe"]
        if not recipe or recipe.get("source") != "rakuten_recipe":
            raise ValueError("献立案のレシピを確認してください。")
        detail = lookup.get(recipe["recipe_id"])
        if detail is None:
            detail = unknown_detail(recipe)
        url = source_link(detail, recipe["recipe_id"])
        if not detail.get("ingredients"):
            detail = unknown_detail(recipe)
        ingredients = scale_ingredients(detail, servings)
        meals.append({"date": meal["date"], "weekday": meal["weekday"],
                      "recipe_id": recipe["recipe_id"], "title": recipe["title"],
                      "author": recipe.get("author", ""), "source_url": url,
                      "source_servings": detail.get("source_servings"), "verified_on": detail.get("verified_on", ""),
                      "automatically_read": detail.get("automatically_read", False),
                      "ingredients": ingredients, "notes": detail.get("notes", []),
                      "price_reasons": recipe.get("price_reasons", [])})
        for item in ingredients:
            total = totals.setdefault(item["name"], {"numeric": defaultdict(Fraction), "approximate": False,
                                                    "as_needed": [], "used_on": []})
            if meal["weekday"] not in total["used_on"]:
                total["used_on"].append(meal["weekday"])
            if item["amount"] is None:
                total["as_needed"].append({"weekday": meal["weekday"], "note": item["note"]})
            else:
                amount, unit = Fraction(item["amount"]), item["unit"]
                if unit == "大さじ":
                    amount, unit = amount * 3, "小さじ"
                total["numeric"][unit] += amount
                total["approximate"] |= item["note"] in ("約", "目安")
    shopping = []
    for name, total in totals.items():
        text = "＋".join(amount_text(amount, unit) for unit, amount in total["numeric"].items())
        if total["approximate"]:
            text += "（目安）"
        if total["as_needed"]:
            text += ("＋" if text else "") + "不明"
        shopping.append({"name": name, "display": text,
                         "quantities": {unit: str(amount) for unit, amount in total["numeric"].items()},
                         "approximate": total["approximate"], "as_needed": total["as_needed"],
                         "used_on": total["used_on"]})
    return {"week_start": plan["week_start"], "target_servings": servings,
            "source_quantities_verified": all(m["ingredients"] and all(i["amount"] is not None for i in m["ingredients"]) for m in meals), "meals": meals, "shopping": shopping}


def source_servings_text(meal):
    value = meal.get("source_servings")
    return f"{value}人分" if type(value) is int and value > 0 else "人数不明"


def render_html(data, template):
    cards, shopping = [], []
    for meal in data["meals"]:
        checked_label = "元レシピ取得" if meal.get("automatically_read") else "分量確認"
        checked = f' {checked_label}：{escape(meal["verified_on"])}' if meal.get("verified_on") else ""
        items = "".join(f"<li>{escape(i['name'])}：{escape(i['display'])}</li>" for i in meal["ingredients"])
        if not items:
            items = "<li>材料・分量：不明（元レシピをご確認ください）</li>"
        notes = "".join(f'<p class="note">{escape(note)}</p>' for note in meal["notes"])
        notes += "".join(f'<p class="note">選んだ理由：{escape(reason)}</p>' for reason in meal.get("price_reasons", []))
        cards.append(f'<article><span class="day">{escape(meal["date"])}（{escape(meal["weekday"])}）</span>'
                     f'<h2>{escape(meal["title"])}</h2><h3>材料（{data["target_servings"]}人分）</h3><ul>{items}</ul>'
                     f'<p class="note">元レシピは{source_servings_text(meal)}。{checked}</p>'
                     f'{notes}<p><a href="{escape(meal["source_url"], quote=True)}" target="_blank" rel="noopener noreferrer">'
                     '楽天レシピで作り方を見る</a></p>'
                     f'<p class="note">レシピ提供：{escape(meal["author"])}</p></article>')
    for item in data["shopping"]:
        extra = " / ".join(i["weekday"] + "：" + i["note"] for i in item["as_needed"])
        key = escape(json.dumps([item["name"], item["quantities"], item["as_needed"]], ensure_ascii=False), quote=True)
        shopping.append(f'<li><label><input type="checkbox" data-key="{key}"><span>{escape(item["name"])}：'
                        f'{escape(item["display"])}'
                        + (f'<small class="note">{escape(extra)}</small>' if extra else "") + '</span></label></li>')
    return Template(template).substitute(
        page_heading=f'{data["week_start"]}週の夕飯',
        page_intro=f'{data["target_servings"]}人分・主菜{len(data["meals"])}日分。換算できる材料だけ比例換算しています。人数・分量が読み取れない材料は「不明」です。調理時は元レシピをご確認ください。',
        meal_cards="\n".join(cards), shopping_items="\n".join(shopping))


def render_markdown(data):
    lines = [f'# {data["week_start"]}週の買い物リスト（{data["target_servings"]}人分）', "",
             '分量は必要量です。パック単位などの購入量には切り上げていません。', ""]
    for item in data["shopping"]:
        line = f'- [ ] {item["name"]}：{item["display"]}'
        if item["as_needed"]:
            line += "（" + " / ".join(i["weekday"] + "：" + i["note"] for i in item["as_needed"]) + "）"
        lines.append(line)
    lines += ["", "元レシピ・分量確認日：", ""]
    for meal in data["meals"]:
        lines.append(f'- {meal["weekday"]}：[元レシピ（{source_servings_text(meal)}）]({meal["source_url"]}) / {meal["verified_on"]}')
    return "\n".join(lines) + "\n"


def generate_page(base_dir=BASE_DIR):
    plan = json.loads((base_dir / "data/weekly_plan.json").read_text(encoding="utf-8-sig"))
    details = json.loads((base_dir / "data/recipe_details.json").read_text(encoding="utf-8-sig"))
    data = assemble(plan, details)
    template = (base_dir / "templates/weekly.html").read_text(encoding="utf-8")
    html, markdown = render_html(data, template), render_markdown(data)
    output = base_dir / "output/weekly-preview.html"
    output.parent.mkdir(exist_ok=True)
    # 全件の検証と描画が成功してから保存する。
    for path, content in [(output, html), (base_dir / "data/shopping_list.md", markdown),
                          (base_dir / "data/shopping_list.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")]:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return output


def main():
    try:
        path = generate_page()
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"買い物リストを生成できませんでした: {error}", file=sys.stderr)
        return 1
    print(f"3人分の献立・買い物リストを生成しました: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

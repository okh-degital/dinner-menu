"""確認済みの元分量を人数換算し、買い物リスト付きの献立プレビューを生成。"""

import json
import sys
from collections import defaultdict
from fractions import Fraction
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
UNITS = {"g", "ml", "個", "枚", "束", "袋", "本", "丁", "大さじ", "小さじ"}


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
    return unit + number(amount) if unit in ("大さじ", "小さじ") else number(amount) + unit


def source_link(detail, recipe_id):
    url = detail.get("source_url", "")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "recipe.rakuten.co.jp"
            or parsed.path != f"/recipe/{recipe_id}/" or parsed.query or parsed.fragment):
        raise ValueError("確認データの出典URLとレシピIDが一致しません。")
    return url


def scale_ingredients(detail, servings):
    base = detail.get("source_servings")
    if (detail.get("verified") is not True or type(base) is not int or base <= 0
            or type(servings) is not int or servings <= 0):
        raise ValueError("人数と元の分量が確認できないレシピは換算できません。")
    if not detail.get("ingredients"):
        raise ValueError("確認済みの材料がありません。")
    result = []
    for item in detail["ingredients"]:
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError("材料名を確認してください。")
        note, unit = item.get("note", ""), item.get("unit", "")
        if not isinstance(note, str):
            raise ValueError("材料の注記を確認してください。")
        if item.get("amount") is None:
            if unit or not any(word in note for word in ("少々", "適量")):
                raise ValueError("数量不明の材料があります。確認データに分量を追加してください。")
            amount, text = None, note
        else:
            if unit not in UNITS:
                raise ValueError("未対応の単位です。単位を確認してください。")
            try:
                amount = Fraction(str(item["amount"])) * Fraction(servings, base)
            except (ValueError, ZeroDivisionError):
                raise ValueError("材料の数量を確認してください。") from None
            if amount <= 0:
                raise ValueError("材料の数量は正の数で指定してください。")
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
            raise ValueError(f"分量未確認のレシピがあります: {recipe['recipe_id']}。元レシピを確認してください。")
        url = source_link(detail, recipe["recipe_id"])
        ingredients = scale_ingredients(detail, servings)
        meals.append({"date": meal["date"], "weekday": meal["weekday"],
                      "recipe_id": recipe["recipe_id"], "title": recipe["title"],
                      "author": recipe.get("author", ""), "source_url": url,
                      "source_servings": detail["source_servings"], "verified_on": detail["verified_on"],
                      "ingredients": ingredients, "notes": detail.get("notes", [])})
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
            qualitative = "・".join(word for word in ("適量", "少々")
                                   if any(word in item["note"] for item in total["as_needed"]))
            text += (" ＋ 別途" if text else "") + qualitative
        shopping.append({"name": name, "display": text,
                         "quantities": {unit: str(amount) for unit, amount in total["numeric"].items()},
                         "approximate": total["approximate"], "as_needed": total["as_needed"],
                         "used_on": total["used_on"]})
    return {"week_start": plan["week_start"], "target_servings": servings,
            "source_quantities_verified": True, "meals": meals, "shopping": shopping}


def render_html(data, template):
    cards, shopping = [], []
    for meal in data["meals"]:
        items = "".join(f"<li>{escape(i['name'])}：{escape(i['display'])}</li>" for i in meal["ingredients"])
        notes = "".join(f'<p class="note">{escape(note)}</p>' for note in meal["notes"])
        cards.append(f'<article><span class="day">{escape(meal["date"])}（{escape(meal["weekday"])}）</span>'
                     f'<h2>{escape(meal["title"])}</h2><h3>材料（{data["target_servings"]}人分）</h3><ul>{items}</ul>'
                     f'<p class="note">元レシピは{meal["source_servings"]}人分。分量確認：{escape(meal["verified_on"])}</p>'
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
        page_intro=f'{data["target_servings"]}人分・主菜{len(data["meals"])}日分。元レシピの人数から材料を比例換算しています。',
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
        lines.append(f'- {meal["weekday"]}：[元レシピ（{meal["source_servings"]}人分）]({meal["source_url"]}) / {meal["verified_on"]}')
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

"""楽天の公開レシピの構造化データから、解釈が一意な材料・分量だけ取得する。"""

import json
import re
import unicodedata
from fractions import Fraction
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from build_shopping import UNITS, scale_ingredients


def normalized(text):
    return unicodedata.normalize("NFKC", str(text)).strip()


class RecipeDocument(HTMLParser):
    def __init__(self):
        super().__init__()
        self.canonical = None
        self.blocks = []
        self.parts = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href")
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.parts = []

    def handle_data(self, text):
        if self.parts is not None:
            self.parts.append(text)

    def handle_endtag(self, tag):
        if tag == "script" and self.parts is not None:
            self.blocks.append("".join(self.parts))
            self.parts = None


def recipes_in(value):
    if isinstance(value, list):
        for item in value:
            yield from recipes_in(item)
    elif isinstance(value, dict):
        kind = value.get("@type", [])
        if kind == "Recipe" or isinstance(kind, list) and "Recipe" in kind:
            yield value
        if "@graph" in value:
            yield from recipes_in(value["@graph"])


def ingredient(name, quantity, servings):
    raw_name, raw_quantity = name, quantity
    name = re.sub(r"^[★☆●○◎◆◇■□△▲・*\s]+", "", normalized(name))
    name = {"醤油": "しょうゆ", "醬油": "しょうゆ", "しょう油": "しょうゆ", "鶏挽肉": "鶏ひき肉",
            "塩コショウ": "塩・こしょう", "塩こしょう": "塩・こしょう"}.get(name, name)
    quantity = re.sub(r"\s+", "", normalized(quantity)).replace("cc", "ml").replace("センチ", "cm")
    if not name:
        raise ValueError("材料名が空欄です。")
    if quantity.startswith("各"):
        names = re.split(r"[、・,]", name)
        if len(names) < 2 or not all(names):
            raise ValueError("「各」の対象を特定できません。")
        return [row for n in names for row in ingredient(n, quantity[1:], servings)]
    base = {"name": name, "original_name": raw_name, "original_quantity": raw_quantity}
    if quantity in ("適量", "少々", "少量"):
        return [dict(base, amount=None, unit="", note=quantity)]
    if quantity == "人数分":
        return [dict(base, amount=str(servings), unit="人分", note="")]
    # 単位を明記した表記だけ同じ単位にまとめる。cmをgなどへ推測換算しない。
    quantity = re.sub(r"切$", "切れ", quantity)
    quantity = re.sub(r"^お?茶碗(.+)杯(?:分)?$", r"\1茶碗杯", quantity)
    quantity = quantity.replace("ひとつかみ", "1つかみ")
    quantity = re.sub(r"膳分$", "膳", quantity)
    mixed = re.search(r"(\d+)と(\d+)/(\d+)", quantity)
    if mixed:
        whole, numerator, denominator = map(int, mixed.groups())
        if denominator == 0:
            raise ValueError("分量の分母が0です。")
        quantity = quantity[:mixed.start()] + str(whole + Fraction(numerator, denominator)) + quantity[mixed.end():]
    if re.fullmatch(r"[大小]\d+(?:/\d+|\.\d+)?", quantity):
        quantity = ("大さじ" if quantity[0] == "大" else "小さじ") + quantity[1:]
    qualifier = re.search(r"\(([^\d()]+)\)$", quantity)
    if qualifier:
        name += "（" + qualifier.group(1) + "）"
        quantity = quantity[:qualifier.start()]
    note = ""
    if re.search(r"(?:程度|ほど|くらい|位)$", quantity):
        quantity, note = re.sub(r"(?:程度|ほど|くらい|位)$", "", quantity), "目安"
    if quantity.startswith("約"):
        quantity, note = quantity[1:], "約"
    elif quantity[:1] in ("小", "中", "大") and not quantity.startswith(("大さじ", "小さじ")):
        quantity, note = quantity[1:], quantity[:1] + "サイズ"
    numeric = r"(?:\d+/\d+|\d+(?:\.\d+)?)"
    units = "|".join(re.escape(u) for u in sorted(UNITS | {"kg"}, key=len, reverse=True))
    match = re.fullmatch(rf"(大さじ|小さじ)({numeric})", quantity)
    if match:
        unit, amount = match.groups()
    else:
        match = re.fullmatch(rf"({numeric})({units})", quantity)
        if not match:
            raise ValueError(f"自動換算できない分量です: {raw_name} / {raw_quantity}")
        amount, unit = match.groups()
    try:
        amount = Fraction(amount)
    except (ValueError, ZeroDivisionError):
        raise ValueError("分量の数値を読み取れません。") from None
    if unit == "kg":
        amount, unit = amount * 1000, "g"
    if amount <= 0:
        raise ValueError("分量は正の値が必要です。")
    # 小サイズなどの区別を合計時にも残す。
    if note.endswith("サイズ"):
        name, note = f"{name}（{note}）", ""
    return [dict(base, name=name, amount=str(amount), unit=unit, note=note)]


def parse_detail(html, recipe, checked_on):
    recipe_id = recipe["recipe_id"]
    url = f"https://recipe.rakuten.co.jp/recipe/{recipe_id}/"
    document = RecipeDocument()
    document.feed(html)
    if document.canonical != url:
        raise ValueError("元レシピのURLが一致しません。")
    found = [r for block in document.blocks for r in recipes_in(json.loads(block))]
    if len(found) != 1 or normalized(found[0].get("name")) != normalized(recipe["title"]):
        raise ValueError("元レシピを一意に確認できません。")
    original = found[0]
    match = re.fullmatch(r"([1-9]\d?)(?:人分)?", normalized(original.get("recipeYield", "")))
    if not match:
        raise ValueError("元の人数が範囲表記などのため、自動換算できません。")
    servings = int(match.group(1))
    names = sorted({normalized(n) for n in recipe["ingredient_names"]}, key=len, reverse=True)
    rows, matched = [], set()
    ingredients = original.get("recipeIngredient")
    if not isinstance(ingredients, list) or not ingredients:
        raise ValueError("元の材料一覧がありません。")
    for text in ingredients:
        text = normalized(text)
        heading = re.fullmatch(r"【[^】]*(?:材料|調味料|たれ|タレ|あん|ソース|トッピング)[^】]*】|[●★◆].+の材料", text)
        if text in names and heading:
            # 元ページとAPI双方にある、量を持たない調味料グループの見出し。
            matched.add(text)
            continue
        name = next((n for n in names if text.startswith(n + " ")), None)
        if name is None:
            raise ValueError("APIの材料と元レシピの材料が一致しません。")
        matched.add(name)
        rows.extend(ingredient(name, text[len(name):].strip(), servings))
    if matched != set(names):
        raise ValueError("元レシピに不足している材料があります。")
    detail = {"recipe_id": recipe_id, "source_url": url, "source_servings": servings,
              "ingredients": rows, "verified": True, "verified_on": checked_on.isoformat(),
              "automatically_read": True, "verification_method": "元レシピの構造化データとAPIの材料名を照合",
              "notes": ["元レシピの材料欄から自動取得しています。調理手順は出典をご確認ください。"]}
    scale_ingredients(detail, 3)
    return detail


def fetch_detail(recipe, checked_on):
    if not re.fullmatch(r"\d+", recipe["recipe_id"]):
        raise ValueError("レシピIDが不正です。")
    url = f'https://recipe.rakuten.co.jp/recipe/{recipe["recipe_id"]}/'
    request = Request(url, headers={"User-Agent": "DinnerMenu/1.0"})
    try:
        with urlopen(request, timeout=25) as response:
            if response.url != url:
                raise ValueError("元レシピのURLが変更されています。")
            body = response.read(3_000_001)
            if len(body) > 3_000_000:
                raise ValueError("元レシピのデータが大きすぎます。")
        return parse_detail(body.decode("utf-8"), recipe, checked_on)
    except HTTPError as error:
        raise ValueError(f"元レシピを取得できません（HTTP {error.code}）。") from None
    except (URLError, TimeoutError, UnicodeError):
        raise ValueError("元レシピに接続できませんでした。") from None

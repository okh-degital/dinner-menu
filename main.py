"""仮の献立から夕飯ページを生成する最小版（外部ライブラリ不要）。"""

import json
from collections import defaultdict
from html import escape
from pathlib import Path
from string import Template

BASE_DIR = Path(__file__).resolve().parent

# 元のレシピは2人分。出力時に1.5倍して3人分にします。レシピ取得・履歴への記録は後のステップで追加します。
RECIPES = {
    "月": {
        "name": "豚の生姜焼き",
        "ingredients": [("豚薄切り肉", 200, "g"), ("玉ねぎ", 1, "個"),
                        ("しょうゆ", 1, "大さじ"), ("みりん", 1, "大さじ"),
                        ("しょうが", 1, "小さじ"), ("油", 1, "小さじ")],
        "steps": ["玉ねぎを薄切りにする。", "フライパンに油を熱し、豚肉と玉ねぎを炒める。",
                  "肉にしっかり火が通ったら、しょうゆ・みりん・しょうがを加えてからめる。"],
    },
    "火": {
        "name": "豆腐とひき肉の甘辛炒め",
        "ingredients": [("木綿豆腐", 1, "丁"), ("豚ひき肉", 150, "g"),
                        ("しょうゆ", 1, "大さじ"), ("みりん", 1, "大さじ"), ("油", 1, "小さじ")],
        "steps": ["豆腐の水気を切り、一口大に切る。", "油を熱し、ひき肉をほぐしながら十分に炒める。",
                  "豆腐・しょうゆ・みりんを加え、豆腐が温まるまで炒め合わせる。"],
    },
    "水": {
        "name": "鮭ときのこの蒸し焼き",
        "ingredients": [("生鮭", 2, "切れ"), ("しめじ", 1, "袋"),
                        ("玉ねぎ", 1, "個"), ("酒", 2, "大さじ"), ("しょうゆ", 1, "大さじ")],
        "steps": ["玉ねぎを薄切りにし、しめじをほぐす。", "フライパンに野菜と鮭を入れ、酒をふってふたをする。",
                  "弱めの中火で蒸し焼きにし、鮭の中心まで火が通ったらしょうゆをかける。"],
    },
    "金": {
        "name": "チキンカレー",
        "ingredients": [("鶏もも肉", 200, "g"), ("玉ねぎ", 1, "個"),
                        ("にんじん", 1, "本"), ("じゃがいも", 1, "個"),
                        ("カレールウ", 2, "皿分"), ("油", 1, "小さじ")],
        "steps": ["肉と野菜を食べやすい大きさに切る。", "鍋に油を熱して肉と野菜を炒め、ルウの表示に合わせた量の水を加える。",
                  "肉に十分火が通り、野菜が柔らかくなるまで煮る。火を止めてルウを溶かし、再び弱火で煮る。"],
    },
}


def generate_page(base_dir=BASE_DIR):
    settings = json.loads((base_dir / "settings.json").read_text(encoding="utf-8-sig"))
    weekdays = settings["weekdays"]
    if (not isinstance(weekdays, list) or not weekdays
            or any(not isinstance(day, str) or day not in RECIPES for day in weekdays)
            or len(set(weekdays)) != len(weekdays)):
        raise ValueError("weekdaysには月・火・水・金から重複なしで曜日を指定してください。")

    cards = []
    shopping = defaultdict(int)
    for day in weekdays:
        recipe = RECIPES[day]
        ingredients = []
        for name, amount, unit in recipe["ingredients"]:
            amount *= 1.5
            ingredients.append(f"<li>{escape(name)}：{amount:g}{escape(unit)}</li>")
            shopping[(name, unit)] += amount
        steps = "".join(f"<li>{escape(step)}</li>" for step in recipe["steps"])
        cards.append(
            f'<article><span class="day">{escape(day)}曜日</span>'
            f'<h2>{escape(recipe["name"])}</h2><h3>材料（3人分）</h3>'
            f'<ul>{"".join(ingredients)}</ul><h3>作り方</h3><ol>{steps}</ol></article>'
        )

    shopping_items = []
    for (name, unit), amount in shopping.items():
        key = escape(f"{name}|{amount}|{unit}", quote=True)
        shopping_items.append(
            f'<li><label><input type="checkbox" data-key="{key}">'
            f'<span>{escape(name)}：{amount:g}{escape(unit)}</span></label></li>'
        )
    template = Template((base_dir / "templates" / "index.html").read_text(encoding="utf-8"))
    page = template.substitute(meal_cards="\n".join(cards), shopping_items="\n".join(shopping_items))
    output_dir = base_dir / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "index.html"
    output_path.write_text(page, encoding="utf-8")
    return output_path


def main():
    output_path = generate_page()
    print(f"夕飯ページを生成しました: {output_path}")


if __name__ == "__main__":
    main()

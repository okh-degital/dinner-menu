"""農水省の最新掲載資料から、野菜・肉・卵の全国平均価格を取得。"""

import io
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone, date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from food_prices import price_settings, render_report

BASE_DIR = Path(__file__).resolve().parent
SOURCES = {
    "vegetables": ("農水省・野菜", "https://www.maff.go.jp/j/zyukyu/anpo/kouri/k_yasai/h22index.html"),
    "meat_eggs": ("農水省・食肉と鶏卵", "https://www.maff.go.jp/j/zyukyu/anpo/kouri/k_gyuniku/"),
}
VEGETABLES = "キャベツ ねぎ レタス ばれいしょ たまねぎ きゅうり トマト にんじん はくさい だいこん".split()
MEAT_HEADERS = ["輸入牛肉", "国産牛肉", "豚肉", "鶏肉", "鶏卵"]
MEAT_KEYS = ["輸入牛ロース", "国産牛ロース", "豚ロース", "鶏もも肉", "卵"]
MEAT_LABELS = ["輸入牛肉（ロース）", "国産牛肉（ロース）", "豚肉（ロース）", "鶏肉（もも肉）", "鶏卵（サイズ混合10個）"]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.href, self.parts = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href, self.parts = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self.href:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((self.href, "".join(self.parts)))
            self.href = None


def latest_pdf(html, base_url):
    parser = Links()
    parser.feed(html)
    candidates = []
    for href, label in parser.links:
        label = unicodedata.normalize("NFKC", label)
        match = re.search(r"令和\s*(\d+)年\s*(\d+)月\s*(\d+)日", label)
        url = urljoin(base_url, href)
        parsed = urlsplit(url)
        if (match and parsed.scheme == "https" and parsed.netloc == "www.maff.go.jp"
                and parsed.path.startswith("/j/zyukyu/anpo/kouri/") and parsed.path.endswith(".pdf")
                and "過去" not in label and "注" not in label):
            year, month, day = map(int, match.groups())
            candidates.append((date(2018 + year, month, day), url))
    if not candidates:
        raise ValueError("最新調査のPDFリンクを確認できません。")
    return max(candidates)


def parse_table(text, group, survey_date, source_url):
    text = unicodedata.normalize("NFKC", text)
    compact = re.sub(r"\s+", "", text)
    heading = re.search(r"令和(\d+)年(\d+)月(?:(\d+)日の週|\((\d+)月(\d+)日[~〜～])", compact)
    if not heading:
        raise ValueError("資料の調査日を確認できません。")
    year, month, day, inner_month, inner_day = heading.groups()
    parsed_date = date(2018 + int(year), int(inner_month or month), int(day or inner_day))
    if parsed_date != survey_date:
        raise ValueError("リンクと資料の調査日が一致しません。")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    allowed = VEGETABLES if group == "vegetables" else MEAT_HEADERS
    header_lines = [line for line in lines if all(name in line for name in (["キャベツ", "レタス"] if group == "vegetables" else MEAT_HEADERS))]
    if len(header_lines) != 1:
        raise ValueError("価格表の列見出しを確認できません。")
    header = header_lines[0]
    names = sorted([name for name in allowed if name in header], key=header.index)
    if len(names) != (8 if group == "vegetables" else 5):
        raise ValueError("調査品目が変わったため列の対応を確認してください。")
    if group == "vegetables" and "円/kg" not in compact:
        raise ValueError("野菜の価格単位が変わっています。")
    if group == "meat_eggs" and ("円/100g" not in compact or compact.count("(ロース)") != 3
                                or "(もも肉)" not in compact or "10個入り" not in compact):
        raise ValueError("肉・卵の調査規格が変わっています。")
    def row(label, percent=False):
        found = []
        for line in lines:
            match = re.search(r"(?:^|\s)" + label + r"\s+(.+)$", line)
            if match:
                tokens = match.group(1).split()
                pattern = r"\d+(?:\.\d+)?%" if percent else r"\d[\d,]*"
                if len(tokens) == len(names) and all(re.fullmatch(pattern, token) for token in tokens):
                    found.append([float(token.rstrip("%").replace(",", "")) for token in tokens])
        if len(found) != 1:
            raise ValueError(f"価格表の{label}を確認できません。")
        return found[0]
    amounts, ratios = row("価格"), row("平年比", True)
    if any(v <= 0 for v in amounts) or any(not 0 < v < 1000 for v in ratios):
        raise ValueError("価格表の数値が不正です。")
    items = []
    for name, amount, ratio in zip(names, amounts, ratios):
        index = MEAT_HEADERS.index(name) if group == "meat_eggs" else None
        items.append({"ingredient": MEAT_KEYS[index] if index is not None else name,
                      "label": MEAT_LABELS[index] if index is not None else name,
                      "price_yen": int(amount) if amount.is_integer() else amount,
                      "unit": "10個" if name == "鶏卵" else "100g" if index is not None else "kg",
                      "normal_percent": int(ratio) if ratio.is_integer() else ratio,
                      "survey_date": survey_date.isoformat(), "group": group, "source_url": source_url})
    return items


def download(url):
    request = Request(url, headers={"User-Agent": "DinnerMenu/1.0"})
    with urlopen(request, timeout=20) as response:
        content = response.read(5_000_001)
    if len(content) > 5_000_000:
        raise ValueError("価格資料のサイズを確認してください。")
    return content


def refresh_prices(base_dir=BASE_DIR):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("価格調査には .venv\\Scripts\\python.exe を使ってください。") from None
    data = {"fetched_at": datetime.now(timezone.utc).isoformat(), "items": [], "sources": [], "warnings": []}
    for group, (label, page_url) in SOURCES.items():
        try:
            survey_date, url = latest_pdf(download(page_url).decode("utf-8-sig"), page_url)
            pdf = PdfReader(io.BytesIO(download(url)))
            text = pdf.pages[0].extract_text(extraction_mode="layout")
            items = parse_table(text, group, survey_date, url)
            data["items"].extend(items)
            data["sources"].append({"label": label, "url": url, "survey_date": survey_date.isoformat()})
        except Exception as error:
            # 通信・PDF形式の変更は通常の候補選びを妨げない。前回値を新しい値と混ぜない。
            data["warnings"].append(f"{label}を取得・確認できませんでした。この区分は加点しません。（{type(error).__name__}）")
    path = base_dir / "data/food_prices.json"
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return data


def main():
    try:
        settings = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8-sig"))
        config = price_settings(settings)
        data = refresh_prices()
        today = datetime.now(timezone(timedelta(hours=9))).date()
        (BASE_DIR / "data/food_prices.md").write_text(render_report(data, config, today), encoding="utf-8")
        print(f"農水省の価格を{len(data['items'])}品目取得しました。調査日・判定は data/food_prices.md で確認できます。")
        for warning in data["warnings"]:
            print(warning)
        return 0 if data["items"] else 1
    except (ValueError, OSError) as error:
        print(f"価格情報を更新できませんでした: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

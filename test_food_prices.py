import copy
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fetch_food_prices import latest_pdf, parse_table, refresh_prices
from food_prices import assess, cheap_categories, load_prices, main_ingredients, price_settings, score_recipe
from plan_week import build_plan
from select_dinner import category_rows, classify, render_candidates, meal_preferences, preferred_categories

DAY = date(2026, 9, 28)
CONFIG = price_settings({})
VEG_TEXT = """食品価格動向調査(野菜)の調査結果
令和８年９月７日の週【９月７日～９月９日】の調査結果
（単位：円/kg）
品目 キャベツ ねぎ レタス ばれいしょ たまねぎ きゅうり トマト にんじん
価格 197 916 500 489 339 760 967 465
前週比 96% 99% 110% 94% 94% 101% 102% 100%
平年比 120% 99% 71% 109% 104% 102% 99% 93%
"""


def item(name="キャベツ", ratio=90, age=0, group="vegetables"):
    return {"ingredient": name, "normal_percent": ratio, "survey_date": (DAY - timedelta(days=age)).isoformat(),
            "group": group}


def recipe(key, ingredients, title="炒め物", score=3):
    return {"source": "rakuten_recipe", "recipe_id": key, "title": title,
            "ingredient_names": ingredients, "status": "candidate", "score": score}


class FoodPriceTests(unittest.TestCase):
    def test_salads_excluded_and_other_staples_stay_on_hold(self):
        for title in ("鶏肉とレタスのサラダ", "サラダ丼", "ポテトサラダ"):
            self.assertEqual(classify(recipe("a", ["鶏肉", "レタス"], title))[0], "excluded")
        self.assertEqual(classify(recipe("t", ["鶏肉"], "野菜たっぷりトルティーヤ"))[0], "hold")
        self.assertEqual(classify(recipe("b", ["レタス", "豚肉"], "レタスと豚肉の炒め物"))[0], "candidate")
        self.assertEqual(classify(recipe("c", ["豚肉"], "豚スライス肉をサラダ油で焼く"))[0], "candidate")

    def test_rice_bowls_preferred_and_egg_bowl_is_eligible(self):
        for title, protein in (("家にあるもので簡単タコライス♪", "合いびき肉"), ("牛丼", "牛肉"),
                               ("親子丼", "鶏肉"), ("玉子丼", "卵"), ("豚どんぶり", "豚肉")):
            dish = recipe("a", [protein, "ご飯"], title)
            status, score, reasons = classify(dish)
            self.assertEqual(status, "candidate")
            self.assertEqual(score, classify(dish, {"rice_bowls": "allow"})[1] + 2)
            self.assertTrue(any("丼物・タコライスを優先" in reason for reason in reasons))
            self.assertEqual(classify(dish, {"rice_bowls": "hold"})[0], "hold")

    def test_bowl_preference_survives_planning_but_never_overrides_history(self):
        ordinary = recipe("a", ["牛肉"], "牛肉炒め")
        bowl = recipe("b", ["牛肉", "ご飯"], "牛丼")
        for dish in (ordinary, bowl):
            dish["status"], dish["score"], dish["reasons"] = classify(dish)
            dish["base_score"] = dish["score"]
            dish["preference_reasons"] = [r for r in dish["reasons"] if "丼物・タコライスを優先" in r]
        report = {"source_fetched_at": "test", "recipes": [ordinary, bowl]}
        plan = build_plan(report, [], ["月"], DAY)
        self.assertEqual(plan["meals"][0]["recipe"]["recipe_id"], "b")
        self.assertTrue(any("丼物・タコライスを優先" in r for r in plan["meals"][0]["selection_reasons"]))
        history = [{"date": "2026-09-27", "source": "rakuten_recipe", "recipe_id": "b"}]
        self.assertEqual(build_plan(report, history, ["月"], DAY)["meals"][0]["recipe"]["recipe_id"], "a")

    def test_preferred_category_names_select_only_one_level_each(self):
        categories = [{"categoryId": "14-124", "categoryName": "タコライス"},
                      {"categoryId": "14-130", "categoryName": "丼物"},
                      {"categoryId": "14-124-568", "categoryName": "タコライス"}]
        self.assertEqual([c["categoryId"] for c in preferred_categories(categories, meal_preferences({}))],
                         ["14-130", "14-124"])
        self.assertEqual(preferred_categories(categories, {"rice_bowls": "hold"}), [])
        with self.assertRaises(ValueError):
            meal_preferences({"meal_preferences": {"rice_bowls": "unknown"}})

    def test_readable_candidates_use_canonical_links_and_preserve_reasons(self):
        dish = recipe("12345", ["レタス", "豚肉"], "レタス炒め")
        dish.update(url="https://recipe.rakuten.co.jp/recipe/12345/?rafcid=private-example",
                    reasons=["レタスが平年比80％で割安（全国平均・2026-09-28調査）"], main_ingredients=["レタス"])
        report = {"source_fetched_at": "2026-09-28", "recipes": [dish], "categories": [{"name": "レタス"}]}
        output = render_candidates(report, {"items": [item("レタス", 80)]}, CONFIG, DAY)
        self.assertIn("https://recipe.rakuten.co.jp/recipe/12345/)", output)
        self.assertIn("平年比80％", output)
        self.assertNotIn("private-example", output)
        self.assertIn("分量確認はまだ行っていません", output)

    def test_table_keeps_price_ratio_and_column_alignment(self):
        rows = parse_table(VEG_TEXT, "vegetables", date(2026, 9, 7), "https://www.maff.go.jp/test.pdf")
        lettuce = next(r for r in rows if r["ingredient"] == "レタス")
        self.assertEqual((len(rows), lettuce["price_yen"], lettuce["normal_percent"], lettuce["unit"]), (8, 500, 71, "kg"))
        self.assertEqual(rows[-1]["normal_percent"], 93)

    def test_ambiguous_table_wrong_date_and_wrong_units_fail_closed(self):
        for text in (VEG_TEXT.replace("500", "—"), VEG_TEXT.replace("71%", "—"),
                     VEG_TEXT.replace("円/kg", "円/個"), VEG_TEXT.replace("レタス", "不明な野菜")):
            with self.assertRaises(ValueError):
                parse_table(text, "vegetables", date(2026, 9, 7), "url")
        with self.assertRaises(ValueError):
            parse_table(VEG_TEXT, "vegetables", date(2026, 9, 14), "url")

    def test_latest_official_link_selected_by_date_not_order(self):
        html = '<a href="attach/pdf/old.pdf">令和8年9月7日の週</a><a href="attach/pdf/new.pdf">令和８年９月１４日の週</a>'
        html += '<a href="https://example.com/fake.pdf">令和8年10月1日の週</a>'
        day, url = latest_pdf(html, "https://www.maff.go.jp/j/zyukyu/anpo/kouri/k_yasai/h22index.html")
        self.assertEqual(day, date(2026, 9, 14))
        self.assertTrue(url.endswith("/new.pdf"))

    def test_threshold_and_expiry_boundaries(self):
        self.assertEqual(assess(item(ratio=90, age=14), CONFIG, DAY), "cheap")
        self.assertEqual(assess(item(ratio=91), CONFIG, DAY), "normal")
        for age in (15, -1):
            self.assertEqual(assess(item(age=age), CONFIG, DAY), "stale")
        self.assertEqual(assess(item("卵", age=45, group="meat_eggs"), CONFIG, DAY), "cheap")
        self.assertEqual(assess(item("卵", age=46, group="meat_eggs"), CONFIG, DAY), "stale")

    def test_garnish_and_processed_ingredients_do_not_gain_bonus(self):
        prices = {"items": [item("レタス"), item("卵"), item("トマト")]}
        dish = recipe("shrimp", ["エビ", "レタス(付け合わせ)", "卵", "トマトケチャップ"], "海老マヨ")
        self.assertEqual(score_recipe(dish, prices, CONFIG, DAY)[0], 0)
        self.assertEqual(score_recipe(recipe("a", ["レタス", "豚肉"], "レタス炒め"), prices, CONFIG, DAY)[0], 2)

    def test_meat_cut_must_match_and_vegetable_aliases_work(self):
        prices = {"items": [item("鶏もも肉", group="meat_eggs"), item("たまねぎ")]}
        self.assertEqual(score_recipe(recipe("a", ["鶏むね肉"]), prices, CONFIG, DAY)[0], 0)
        self.assertEqual(score_recipe(recipe("b", ["鶏もも肉"]), prices, CONFIG, DAY)[0], 2)
        self.assertIn("たまねぎ", main_ingredients(recipe("c", ["玉葱", "豚肉"])))

    def test_expired_cached_bonus_is_removed_when_planning(self):
        fresh = recipe("fresh", ["豚肉"], score=4)
        old = dict(recipe("old", ["キャベツ", "豚肉"]), base_score=3, score=100, price_bonus=97)
        report = {"source_fetched_at": "test", "recipes": [old, fresh]}
        plan = build_plan(report, [], ["月"], DAY, {"items": [item(age=15)]}, CONFIG)
        self.assertEqual(plan["meals"][0]["recipe"]["recipe_id"], "fresh")

    def test_cheap_recipe_wins_but_history_remains_hard_constraint(self):
        recipes = [recipe("a", ["豚肉"]), recipe("b", ["キャベツ", "豚肉"])]
        report = {"source_fetched_at": "test", "recipes": recipes}
        prices = {"items": [item()]}
        original = copy.deepcopy(report)
        plan = build_plan(report, [], ["月"], DAY, prices, CONFIG)
        self.assertEqual(plan["meals"][0]["recipe"]["recipe_id"], "b")
        self.assertIn("平年比90", plan["meals"][0]["selection_reasons"][-1])
        history = [{"date": "2026-09-27", "source": "rakuten_recipe", "recipe_id": "b"}]
        plan = build_plan(report, history, ["月"], DAY, prices, CONFIG)
        self.assertEqual(plan["meals"][0]["recipe"]["recipe_id"], "a")
        self.assertEqual(report, original)

    def test_expiry_rechecked_for_each_meal_date(self):
        report = {"source_fetched_at": "test", "recipes": [recipe("a", ["キャベツ", "豚肉"])]}
        plan = build_plan(report, [], ["月", "金"], DAY, {"items": [item(age=14)]}, CONFIG)
        monday = plan["meals"][0]["recipe"]
        self.assertIsNotNone(monday)
        self.assertEqual(monday["price_bonus"], 2)

    def test_cheap_ingredient_does_not_fill_all_four_days(self):
        recipes = [recipe(f"c{i}", ["キャベツ", "豚肉"]) for i in range(4)]
        recipes += [recipe(f"p{i}", ["豚肉"]) for i in range(2)]
        report = {"source_fetched_at": "test", "recipes": recipes}
        plan = build_plan(report, [], ["月", "火", "水", "金"], DAY, {"items": [item()]}, CONFIG)
        self.assertEqual(plan["status"], "draft_ready")
        self.assertEqual(sum("キャベツ" in m["recipe"]["main_ingredients"] for m in plan["meals"]), 2)

    def test_meat_regulation_and_units_are_preserved(self):
        text = """令和８年９月（９月７日～９月９日）の調査結果
（単位：円/100g（鶏卵は円/1パック））
輸入牛肉 国産牛肉 豚肉 鶏肉 鶏卵
(ロース) (ロース) (ロース) (もも肉) (サイズ混合・10個入り)
価格 442 859 293 155 309
平年比 126% 103% 107% 113% 119%
"""
        rows = parse_table(text, "meat_eggs", date(2026, 9, 7), "url")
        self.assertEqual((rows[3]["ingredient"], rows[3]["price_yen"], rows[4]["unit"]), ("鶏もも肉", 155, "10個"))
        with self.assertRaises(ValueError):
            parse_table(text.replace("もも肉", "むね肉"), "meat_eggs", date(2026, 9, 7), "url")

    def test_adds_at_most_two_cheap_categories_with_full_ids(self):
        rows = category_rows({"medium": [
            {"categoryId": 1, "categoryName": "キャベツ", "categoryUrl": "https://recipe.rakuten.co.jp/category/12-1/"},
            {"categoryId": 2, "categoryName": "レタス", "categoryUrl": "https://recipe.rakuten.co.jp/category/12-2/"},
            {"categoryId": 3, "categoryName": "トマト", "categoryUrl": "https://recipe.rakuten.co.jp/category/12-3/"}]})
        prices = {"items": [item("キャベツ", 80), item("レタス", 70), item("トマト", 85)]}
        self.assertEqual([c["categoryId"] for c in cheap_categories(prices, CONFIG, DAY, rows)], ["12-2", "12-1"])
        self.assertEqual(cheap_categories(prices, {**CONFIG, "enabled": False}, DAY, rows), [])

    def test_connection_failure_clears_old_price_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data/food_prices.json").write_text('{"items": [{"ingredient": "キャベツ"}]}', encoding="utf-8")
            with patch("fetch_food_prices.download", side_effect=OSError("offline")):
                result = refresh_prices(root)
            self.assertEqual(result["items"], [])
            self.assertEqual(len(result["warnings"]), 2)
            self.assertEqual(load_prices(root)["items"], [])

    def test_invalid_settings_and_corrupt_cache(self):
        for config in ({"priority": "unknown"}, {"cheap_below_normal_percent": True}, {"max_extra_categories": 100}):
            with self.assertRaises(ValueError):
                price_settings({"food_prices": config})
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_prices(Path(directory))["items"], [])
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data/food_prices.json").write_text('{"items": ["broken"]}', encoding="utf-8")
            self.assertEqual(load_prices(root)["items"], [])
            self.assertTrue(load_prices(root)["warnings"])


if __name__ == "__main__":
    unittest.main()

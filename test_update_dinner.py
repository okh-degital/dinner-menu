import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import update_dinner as update
from recipe_details import ingredient, parse_detail
import test_build_shopping as shopping_fixtures


class RecipeDetailsTests(unittest.TestCase):
    def recipe(self):
        return {"recipe_id": "123", "title": "しょうが焼き", "ingredient_names": ["豚肉", "★醤油", "ご飯"]}

    def page(self, **changes):
        data = {"@type": "Recipe", "name": "しょうが焼き", "recipeYield": "２",
                "recipeIngredient": ["豚肉 ２００ｇ", "★醤油 大さじ１／２", "ご飯 人数分"]}
        data.update(changes)
        return '<link rel="canonical" href="https://recipe.rakuten.co.jp/recipe/123/">' + \
            '<script type="application/ld+json">' + json.dumps([data]) + '</script>'

    def test_exact_original_quantities_and_servings(self):
        result = parse_detail(self.page(), self.recipe(), date(2026, 9, 27))
        self.assertEqual(result["source_servings"], 2)
        self.assertEqual([(r["name"], r["amount"], r["unit"]) for r in result["ingredients"]],
                         [("豚肉", "200", "g"), ("しょうゆ", "1/2", "大さじ"), ("ご飯", "2", "人分")])
        self.assertTrue(result["automatically_read"])

    def test_range_or_ambiguous_quantity_never_guessed(self):
        for changes in [{"recipeYield": "2〜4"}, {"recipeYield": "たっぷり2"},
                        {"recipeIngredient": ["豚肉 200〜300g", "★醤油 大さじ1", "ご飯 人数分"]}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_detail(self.page(**changes), self.recipe(), date(2026, 9, 27))

    def test_wrong_recipe_and_missing_material_stop(self):
        for page in [self.page().replace('/recipe/123/', '/recipe/999/'),
                     self.page(name="別の料理"), self.page(recipeIngredient=["豚肉 200g"])]:
            with self.assertRaises(ValueError):
                parse_detail(page, self.recipe(), date(2026, 9, 27))

    def test_qualitative_each_sizes_and_kg(self):
        self.assertEqual(ingredient("油", "適量", 2)[0]["amount"], None)
        rows = ingredient("醤油・みりん", "各大さじ2", 2)
        self.assertEqual([r["name"] for r in rows], ["しょうゆ", "みりん"])
        self.assertEqual(ingredient("玉ねぎ", "小1個", 2)[0]["name"], "玉ねぎ（小サイズ）")
        self.assertEqual(ingredient("肉", "0.2kg", 2)[0]["amount"], "200")
        self.assertEqual(ingredient("ねぎ", "4cm", 2)[0]["unit"], "cm")
        self.assertEqual(ingredient("ねぎ", "少量", 2)[0]["note"], "少量")
        self.assertEqual(ingredient("ねぎ", "ひとつかみ", 2)[0]["amount"], "1")
        self.assertEqual(ingredient("生姜", "3cm程度", 2)[0]["note"], "目安")
        self.assertEqual(ingredient("砂糖", "小さじ1と1/2", 2)[0]["amount"], "3/2")
        self.assertEqual(ingredient("ご飯", "3膳分", 2)[0]["unit"], "膳")
        self.assertEqual(ingredient("ご飯", "茶碗2杯分", 2)[0]["unit"], "茶碗杯")
        self.assertEqual(ingredient("鯖", "4切", 2)[0]["unit"], "切れ")
        self.assertEqual(ingredient("しょうゆ", "小2", 2)[0]["unit"], "小さじ")
        self.assertEqual(ingredient("大根", "大さじ3(おろした状態で)", 2)[0]["name"], "大根（おろした状態で）")
        for value in ["1/0g", "0g", "大さじ1弱", "1個(200g)", "お好みで"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ingredient("肉", value, 2)

    def test_named_sauce_heading_not_treated_as_food(self):
        recipe = self.recipe()
        recipe['ingredient_names'].append('【甘酢あん】')
        page = self.page(recipeIngredient=['豚肉 200g', '【甘酢あん】', '★醤油 大さじ1', 'ご飯 人数分'])
        detail = parse_detail(page, recipe, date(2026, 9, 27))
        self.assertEqual(len(detail['ingredients']), 3)
        recipe['ingredient_names'].append('●サルサソースの材料')
        page = self.page(recipeIngredient=['豚肉 200g', '【甘酢あん】', '★醤油 大さじ1',
                                          '●サルサソースの材料', 'ご飯 人数分'])
        self.assertEqual(len(parse_detail(page, recipe, date(2026, 9, 27))['ingredients']), 3)
        recipe['ingredient_names'].append('【にんじん】')
        with self.assertRaises(ValueError):
            parse_detail(page, recipe, date(2026, 9, 27))


class UpdateTests(unittest.TestCase):
    def setUp(self):
        shopping_fixtures.ShoppingTests.setUpClass()
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        for sub in ("data", "output", "templates", "work/data"):
            (self.base / sub).mkdir(parents=True, exist_ok=True)
        self.plan = copy.deepcopy(shopping_fixtures.ShoppingTests.plan)
        for meal in self.plan["meals"]:
            meal["recipe"].update(ingredient_names=["鶏肉"], time_estimate="約10分", cost_estimate="300円前後", score=3)
        self.settings = {"weekdays": ["月", "火", "水", "金"], "food_prices": {"enabled": False}}
        self.history = update.record_plan([], self.plan)
        self.write("settings.json", self.settings)
        self.write("history.json", self.history)
        self.write("data/weekly_plan.json", self.plan)
        self.write("data/recipe_details.json", shopping_fixtures.ShoppingTests.details)
        (self.base / "templates/weekly.html").write_text(
            (update.BASE_DIR / "templates/weekly.html").read_text(encoding="utf-8"), encoding="utf-8")
        (self.base / "output/index.html").write_text("previous", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, data):
        (self.base / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def prepare(self, cached=True):
        return update.prepare(self.base, self.base / "work", date(2026, 9, 28), date(2026, 9, 27), cached)

    def test_recorded_week_reuses_recipes_and_history(self):
        with patch.object(update, "collect", side_effect=AssertionError("must reuse")):
            artifacts, marker = self.prepare()
        self.assertEqual(json.loads(artifacts["history.json"]), self.history)
        self.assertEqual(artifacts["output/index.html"].count('<article>'), 4)
        self.assertIn('材料（3人分）', artifacts["output/index.html"])
        self.assertIn(marker, artifacts["output/index.html"])
        self.assertEqual((self.base / "output/index.html").read_text(), "previous")
        update.install_artifacts(self.base, artifacts)
        again, second_marker = self.prepare()
        self.assertEqual(marker, second_marker)
        self.assertEqual(json.loads(again["history.json"]), self.history)

    def test_new_week_selects_scales_and_records(self):
        self.write("history.json", [])
        pool = {"fetched_at": "2026-09-27", "recipes": [m["recipe"] for m in self.plan["meals"]]}
        self.write("data/dinner_pool.json", pool)
        artifacts, _ = self.prepare()
        self.assertEqual(len(json.loads(artifacts["history.json"])), 4)
        self.assertEqual(len(json.loads(artifacts["data/shopping_list.json"])["meals"]), 4)

    def test_missing_detail_and_insufficient_candidates_preserve_html_and_history(self):
        self.write("history.json", [])
        self.write("data/recipe_details.json", {"recipes": []})
        self.write("data/dinner_pool.json", {"fetched_at": "2026-09-27", "recipes": [m["recipe"] for m in self.plan["meals"]]})
        with self.assertRaisesRegex(ValueError, "そろいません"):
            self.prepare()
        self.assertEqual((self.base / "output/index.html").read_text(), "previous")
        self.assertEqual(update.read_json(self.base / "history.json"), [])

    def test_one_unreadable_recipe_replaced_without_relaxing_rules(self):
        self.write("history.json", [])
        self.write("data/recipe_details.json", {"recipes": []})
        recipes = [dict(m["recipe"], recipe_id=str(i), title=f"料理{i}") for i, m in enumerate(self.plan["meals"], 1)]
        recipes.append(dict(recipes[0], recipe_id="5", title="料理5"))
        pool = {"fetched_at": "2026-09-27", "recipes": recipes}
        def detail(recipe, today):
            if recipe["recipe_id"] == "1":
                raise ValueError("人数が曖昧")
            value = copy.deepcopy(shopping_fixtures.ShoppingTests.details["recipes"][0])
            value.update(recipe_id=recipe["recipe_id"], source_url=f'https://recipe.rakuten.co.jp/recipe/{recipe["recipe_id"]}/')
            return value
        with patch.object(update, "collect", return_value=pool), patch.object(update, "fetch_detail", side_effect=detail), patch.object(update.time, "sleep"):
            artifacts, _ = self.prepare(cached=False)
        plan = json.loads(artifacts["data/weekly_plan.json"])
        self.assertEqual({m["recipe"]["recipe_id"] for m in plan["meals"]}, {"2", "3", "4", "5"})

    def test_partial_or_conflicting_history_not_regenerated(self):
        self.write("history.json", self.history[:1])
        with self.assertRaisesRegex(ValueError, "一致しません"):
            self.prepare()

    def test_transaction_rolls_back_mid_write_and_keeps_backup(self):
        before = (self.base / "history.json").read_bytes()
        original = Path.replace
        def replace(path, target):
            if path.name == "history.json.update-tmp":
                raise OSError("simulated disk failure")
            return original(path, target)
        with patch.object(Path, "replace", replace), self.assertRaises(OSError):
            update.install_artifacts(self.base, {"output/index.html": "new", "history.json": "[]"})
        self.assertEqual((self.base / "output/index.html").read_text(), "previous")
        self.assertEqual((self.base / "history.json").read_bytes(), before)
        self.assertEqual(len(list((self.base / "data/backups").glob('*/manifest.json'))), 1)

    def test_double_click_lock_released_after_failure(self):
        with update.update_lock(self.base):
            with self.assertRaises((ValueError, OSError)):
                with update.update_lock(self.base):
                    self.fail("second run should be blocked")
        with update.update_lock(self.base):
            pass

    def test_publish_uses_only_allowlisted_files(self):
        commands = []
        def fake(base, *args):
            commands.append(args)
            return 'output/index.html' if args[0] == 'diff' else 'abc'
        with patch.object(update, "git", side_effect=fake):
            update.publish(self.base)
        commit = next(c for c in commands if c[0] == 'commit')
        self.assertIn('--only', commit)
        self.assertEqual(commit[-2:], update.PUBLISH_FILES)
        self.assertNotIn('add .', str(commands))

    def test_unrelated_unpushed_commit_blocks_publish(self):
        results = [f'https://github.com/{update.REPOSITORY}.git'] * 2 + ['main', '', '0 1', 'abc', 'rakuten.local.json']
        with patch.object(update, "git", side_effect=results), self.assertRaisesRegex(ValueError, "未送信"):
            update.publication_preflight(self.base)

    def test_publication_requires_marker_not_http_success(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return b'<html>old page</html>'
        with patch.object(update, 'urlopen', return_value=Response()), patch.object(update.time, 'sleep'), \
                patch.object(update.time, 'monotonic', side_effect=[0, 0, 11]):
            self.assertFalse(update.wait_for_public_page('new', seconds=10))


if __name__ == '__main__':
    unittest.main()

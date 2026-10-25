import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import update_dinner as update
from recipe_details import ingredient, parse_detail, fetch_detail
from build_shopping import scale_ingredients
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

    def test_unknown_servings_keeps_names_without_guessing(self):
        for servings in ['2〜4', 'たっぷり2', '', None, ['2'], 0]:
            with self.subTest(servings=servings):
                detail = parse_detail(self.page(recipeYield=servings), self.recipe(), date(2026, 9, 27))
                self.assertIsNone(detail['source_servings'])
                rows = scale_ingredients(detail, 3)
                self.assertEqual([r['name'] for r in rows], ['豚肉', 'しょうゆ', 'ご飯'])
                self.assertTrue(all(r['display'] == '不明' for r in rows))

    def test_ambiguous_or_missing_quantity_only_affects_that_material(self):
        for quantity in ['200〜300g', '大さじ1弱', '1/0g', '0g', '適量', '少々', '少量', 'お好みで', '']:
            with self.subTest(quantity=quantity):
                detail = parse_detail(self.page(recipeIngredient=['豚肉 ' + quantity, '★醤油 大さじ1', 'ご飯 人数分']),
                                      self.recipe(), date(2026, 9, 27))
                rows = scale_ingredients(detail, 3)
                self.assertEqual(rows[0]['display'], '不明')
                self.assertEqual(rows[1]['amount'], '3/2')
                self.assertEqual(rows[2]['display'], '3人分')

    def test_wrong_recipe_stops_reading_but_missing_material_is_unknown(self):
        for page in [self.page().replace('/recipe/123/', '/recipe/999/'), self.page(name='別の料理')]:
            with self.assertRaises(ValueError):
                parse_detail(page, self.recipe(), date(2026, 9, 27))
        detail = parse_detail(self.page(recipeIngredient=['豚肉 200g']), self.recipe(), date(2026, 9, 27))
        rows = scale_ingredients(detail, 3)
        self.assertEqual([r['display'] for r in rows], ['300g', '不明', '不明'])

    def test_missing_mismatched_and_duplicate_source_rows_are_unknown(self):
        for ingredients in [None, [], ['別の材料 200g'], ['豚肉 100g', '豚肉 200g']]:
            with self.subTest(ingredients=ingredients):
                detail = parse_detail(self.page(recipeIngredient=ingredients), self.recipe(), date(2026, 9, 27))
                self.assertTrue(all(r['display'] == '不明' for r in scale_ingredients(detail, 3)))

    def test_network_read_failure_becomes_quantity_failure(self):
        from urllib.error import HTTPError, URLError
        for error in [HTTPError('url', 403, 'forbidden', {}, None), URLError('offline'),
                      TimeoutError(), ConnectionResetError()]:
            with self.subTest(error=type(error).__name__), patch('recipe_details.urlopen', side_effect=error):
                with self.assertRaises(ValueError):
                    fetch_detail(self.recipe(), date(2026, 9, 27))

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
        rows = parse_detail(page, recipe, date(2026, 9, 27))['ingredients']
        self.assertEqual(rows[-1]['name'], '【にんじん】')
        self.assertIsNone(rows[-1]['amount'])


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
        self.write("history.json", [])
        self.write("data/weekly_plan.json", self.plan)
        self.write("data/dinner_pool.json", {"fetched_at": "2026-09-27", "recipes": [m["recipe"] for m in self.plan["meals"]]})
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

    def test_recorded_week_redraws_and_replaces_history(self):
        pool = update.read_json(self.base / "data/dinner_pool.json")
        pool['recipes'] += [dict(r, recipe_id=prefix + r['recipe_id']) for prefix in ['other-', 'third-'] for r in pool['recipes']]
        self.write('data/dinner_pool.json', pool)
        self.write('history.json', self.history)
        previous = {r['recipe_id'] for r in self.history}
        for _ in range(2):
            artifacts, marker = self.prepare()
            plan = json.loads(artifacts['data/weekly_plan.json'])
            chosen = {m['recipe']['recipe_id'] for m in plan['meals']}
            self.assertTrue(chosen.isdisjoint(previous))
            self.assertEqual(len(json.loads(artifacts["history.json"])), 4)
            self.assertEqual(artifacts["output/index.html"].count('<article>'), 4)
            self.assertIn('材料（3人分）', artifacts["output/index.html"])
            self.assertIn(marker, artifacts["output/index.html"])
            update.install_artifacts(self.base, artifacts)
            self.assertEqual({r['recipe_id'] for r in update.read_json(self.base / 'history.json')}, chosen)
            previous |= chosen
        self.assertEqual(len(previous), 12)
        before = (self.base / 'data/weekly_plan.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'そろいません'):
            self.prepare()
        self.assertEqual((self.base / 'data/weekly_plan.json').read_bytes(), before)

    def test_redraw_shortage_preserves_files_and_does_not_publish(self):
        self.write('history.json', self.history)
        names = ['history.json', 'output/index.html', 'data/weekly_plan.json']
        before = {name: (self.base / name).read_bytes() for name in names}
        pool = update.read_json(self.base / 'data/dinner_pool.json')
        # 追加取得でも新しい候補が3品しかなければ、旧献立を復活させない。
        pool['recipes'] += [dict(r, recipe_id='other-' + r['recipe_id']) for r in pool['recipes'][:3]]
        with patch.object(update, 'BASE_DIR', self.base), \
                patch.object(update, 'collect', return_value=pool) as collect, \
                patch.object(update, 'install_artifacts') as install, \
                patch.object(update, 'git') as git, \
                patch.object(update, 'fetch_detail') as fetch:
            self.assertEqual(update.main(['--week-start', '2026-09-28']), 1)
        self.assertEqual(collect.call_count, 2)
        install.assert_not_called()
        git.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual({name: (self.base / name).read_bytes() for name in names}, before)

    def test_new_week_selects_scales_and_records(self):
        self.write("history.json", [])
        pool = {"fetched_at": "2026-09-27", "recipes": [m["recipe"] for m in self.plan["meals"]]}
        self.write("data/dinner_pool.json", pool)
        artifacts, _ = self.prepare()
        self.assertEqual(len(json.loads(artifacts["history.json"])), 4)
        self.assertEqual(len(json.loads(artifacts["data/shopping_list.json"])["meals"]), 4)

    def test_all_missing_details_generate_four_days_from_cache(self):
        self.write('history.json', [])
        (self.base / 'data/recipe_details.json').unlink()
        with patch.object(update, 'fetch_detail') as fetch, patch.object(update, 'collect') as collect:
            artifacts, _ = self.prepare()
        fetch.assert_not_called()
        collect.assert_not_called()
        data = json.loads(artifacts['data/shopping_list.json'])
        self.assertEqual(len(data['meals']), 4)
        self.assertEqual(data['shopping'][0]['display'], '不明')
        self.assertEqual(artifacts['output/index.html'].count('<article>'), 4)
        self.assertIn('鶏肉：不明', artifacts['output/index.html'])
        self.assertIn('鶏肉：不明', artifacts['data/shopping_list.md'])
        self.assertEqual(len(json.loads(artifacts['history.json'])), 4)
        self.assertEqual((self.base / 'output/index.html').read_text(), 'previous')
        self.assertEqual(update.read_json(self.base / 'history.json'), [])
        # 実際に生成ファイルを保存し、そのHTML自体も検査する（テスト用フォルダのみ）。
        update.install_artifacts(self.base, artifacts)
        saved = (self.base / 'output/index.html').read_text(encoding='utf-8')
        self.assertIn('鶏肉：不明', saved)
        self.assertEqual(saved.count('楽天レシピで作り方を見る'), 4)

    def test_all_fetch_failures_keep_selected_ids_without_reselection(self):
        self.write('data/recipe_details.json', {'recipes': []})
        pool = update.read_json(self.base / 'data/dinner_pool.json')
        with patch.object(update, 'collect', return_value=pool) as collect, \
                patch.object(update, 'fetch_detail', side_effect=ValueError('取得失敗')) as fetch, \
                patch.object(update, 'build_plan', wraps=update.build_plan) as build, patch.object(update.time, 'sleep'):
            artifacts, _ = self.prepare(cached=False)
        collect.assert_called_once()
        build.assert_called_once()
        self.assertEqual(fetch.call_count, 4)
        plan = json.loads(artifacts['data/weekly_plan.json'])
        self.assertEqual({m['recipe']['recipe_id'] for m in plan['meals']}, {'1', '2', '3', '4'})
        self.assertTrue(all(r['status'] == 'candidate' for r in json.loads(artifacts['data/dinner_candidates.json'])['recipes']))
        self.assertEqual(artifacts['output/index.html'].count('<article>'), 4)
        self.assertIn('鶏肉：不明', artifacts['output/index.html'])

    def test_unknown_cache_is_retried_on_next_live_run(self):
        self.write('data/recipe_details.json', {'recipes': []})
        artifacts, _ = self.prepare()
        self.write('data/recipe_details.json', json.loads(artifacts['data/recipe_details.json']))
        pool = update.read_json(self.base / 'data/dinner_pool.json')
        lookup = {d['recipe_id']: d for d in shopping_fixtures.ShoppingTests.details['recipes']}
        with patch.object(update, 'collect', return_value=pool), patch.object(update.time, 'sleep'), \
                patch.object(update, 'fetch_detail', side_effect=lambda recipe, today: lookup[recipe['recipe_id']]) as fetch:
            artifacts, _ = self.prepare(cached=False)
        self.assertEqual(fetch.call_count, 4)
        data = json.loads(artifacts['data/shopping_list.json'])
        egg = next(i for i in data['shopping'] if i['name'] == '卵')
        self.assertEqual(egg['display'], '2個')

    def test_four_week_shortage_preserves_existing_html_and_history(self):
        # 27日前のIDは、初回生成でも復活させない。
        old = dict(self.history[0], date="2026-09-01")
        history = [old]
        self.write("history.json", history)
        with self.assertRaisesRegex(ValueError, "そろいません"):
            self.prepare()
        self.assertEqual((self.base / "output/index.html").read_text(), "previous")
        self.assertEqual(update.read_json(self.base / "history.json"), history)

    def test_one_unreadable_recipe_kept_with_other_known_amounts(self):
        self.write("history.json", [])
        self.write("data/recipe_details.json", {"recipes": []})
        recipes = [dict(m["recipe"], recipe_id=str(i), title=f"料理{i}") for i, m in enumerate(self.plan["meals"], 1)]
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
        self.assertEqual({m["recipe"]["recipe_id"] for m in plan["meals"]}, {"1", "2", "3", "4"})
        data = json.loads(artifacts['data/shopping_list.json'])
        unknown = next(m for m in data['meals'] if m['recipe_id'] == '1')
        self.assertEqual(unknown['ingredients'][0]['display'], '不明')
        self.assertTrue(any(i['amount'] is not None for m in data['meals'] for i in m['ingredients']))

    def test_partial_history_regenerated_without_saved_plan(self):
        self.write("history.json", self.history[:1])
        (self.base / "data/weekly_plan.json").unlink()
        pool = update.read_json(self.base / 'data/dinner_pool.json')
        pool['recipes'].append(dict(pool['recipes'][0], recipe_id='extra'))
        self.write('data/dinner_pool.json', pool)
        artifacts, _ = self.prepare()
        self.assertNotIn(self.history[0]['recipe_id'],
                         {r['recipe_id'] for r in json.loads(artifacts['history.json'])})
        self.assertEqual(len(json.loads(artifacts["history.json"])), 4)

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

    def test_open_uses_chrome_for_local_and_public_pages(self):
        for destination in ['file:///F:/menu/index.html', 'https://example.com/?update=123']:
            with self.subTest(destination=destination), \
                    patch.object(Path, 'is_file', return_value=True), \
                    patch.object(update.subprocess, 'Popen') as launch:
                self.assertTrue(update.open_in_chrome(destination))
                command = launch.call_args.args[0]
                self.assertEqual(Path(command[0]).name, 'chrome.exe')
                self.assertEqual(command[1], destination)
                self.assertEqual(len(command), 2)

    def test_missing_chrome_does_not_launch_another_browser(self):
        with patch.object(Path, 'is_file', return_value=False), \
                patch.object(update.subprocess, 'Popen') as launch:
            self.assertFalse(update.open_in_chrome('file:///F:/menu/index.html'))
        launch.assert_not_called()

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

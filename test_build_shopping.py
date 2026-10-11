import copy
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from build_shopping import assemble, amount_text, generate_page, render_html, render_markdown, scale_ingredients

BASE = Path(__file__).resolve().parent


class ShoppingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 実際の換算例を固定したテストデータ。次週の献立や未配布のdata/に依存しない。
        rows = [
            (3, [('卵','2','個',''), ('しょうゆ','1','小さじ','')]),
            (4, [('むき海老','400','g','約'), ('砂糖','1/2','大さじ',''),
                 ('片栗粉','2','大さじ',''), ('サラダ油','3','大さじ','')]),
            (4, [('鶏むね肉','2','枚',''), ('しょうゆ','3','大さじ',''),
                 ('砂糖','4','大さじ',''), ('片栗粉',None,'','適量'), ('サラダ油',None,'','適量')]),
            (2, [('鶏ひき肉','150','g',''), ('玉ねぎ','1/2','個',''), ('豆腐','100','g',''),
                 ('片栗粉','2','大さじ',''), ('しょうゆ','1','大さじ',''),
                 ('砂糖','1','小さじ',''), ('塩',None,'','少々'), ('サラダ油',None,'','適量')])]
        cls.details = {'recipes': [
            {'recipe_id': str(index), 'source_servings': servings, 'verified': True,
             'verified_on': '2026-09-27', 'source_url': f'https://recipe.rakuten.co.jp/recipe/{index}/',
             'ingredients': [dict(name=name, amount=amount, unit=unit, note=note)
                             for name, amount, unit, note in ingredients]}
            for index, (servings, ingredients) in enumerate(rows, 1)]}
        cls.plan = {'status': 'draft_ready', 'week_start': '2026-09-28', 'target_servings': 3,
                    'meals': [{'date': day, 'weekday': weekday,
                               'recipe': {'source': 'rakuten_recipe', 'recipe_id': str(index),
                                          'title': f'確認用{index}', 'author': '確認用'}}
                              for index, (day, weekday) in enumerate(
                                  [('2026-09-28','月'), ('2026-09-29','火'),
                                   ('2026-09-30','水'), ('2026-10-02','金')], 1)]}

    def test_three_person_source_kept_and_four_person_scaled(self):
        egg = scale_ingredients(self.details['recipes'][0], 3)
        self.assertEqual(next(i for i in egg if i['name'] == '卵')['amount'], '2')
        shrimp = scale_ingredients(self.details['recipes'][1], 3)
        self.assertEqual(next(i for i in shrimp if i['name'] == 'むき海老')['amount'], '300')
        self.assertEqual(next(i for i in shrimp if i['name'] == '砂糖')['amount'], '3/8')

    def test_two_person_scaled_and_no_invented_weight(self):
        tofu = scale_ingredients(self.details['recipes'][3], 3)
        self.assertEqual(next(i for i in tofu if i['name'] == '鶏ひき肉')['amount'], '225')
        self.assertEqual(next(i for i in tofu if i['name'] == '玉ねぎ')['amount'], '3/4')
        chicken = scale_ingredients(self.details['recipes'][2], 3)
        self.assertEqual(chicken[0]['amount'], '3/2')
        self.assertEqual(chicken[0]['unit'], '枚')

    def test_spoons_merge_exactly(self):
        data = assemble(self.plan, self.details)
        items = {i['name']: i for i in data['shopping']}
        self.assertEqual(items['しょうゆ']['quantities'], {'小さじ': '49/4'})
        self.assertEqual(items['砂糖']['quantities'], {'小さじ': '93/8'})
        self.assertEqual(amount_text(Fraction('49/4'), '小さじ'), '大さじ4＋小さじ1/4')

    def test_as_needed_not_lost_in_numeric_total(self):
        items = {i['name']: i for i in assemble(self.plan, self.details)['shopping']}
        self.assertEqual(items['片栗粉']['quantities'], {'小さじ': '27/2'})
        self.assertEqual(len(items['片栗粉']['as_needed']), 1)
        self.assertEqual(len(items['サラダ油']['as_needed']), 2)
        self.assertEqual(items['片栗粉']['display'], '大さじ4＋小さじ1と1/2＋不明')
        self.assertNotIn('練乳', items)
        self.assertEqual(items['塩']['display'], '不明')

    def test_missing_or_unverified_keeps_recipe_with_unknown_amounts(self):
        plan = copy.deepcopy(self.plan)
        plan['meals'][-1]['recipe']['ingredient_names'] = ['玉ねぎ', '★醤油']
        details = copy.deepcopy(self.details)
        details['recipes'].pop()
        data = assemble(plan, details)
        self.assertEqual(len(data['meals']), 4)
        self.assertEqual([i['name'] for i in data['meals'][-1]['ingredients']], ['玉ねぎ', 'しょうゆ'])
        self.assertTrue(all(i['display'] == '不明' for i in data['meals'][-1]['ingredients']))
        details = copy.deepcopy(self.details)
        details['recipes'][0]['verified'] = False
        self.assertTrue(all(i['display'] == '不明' for i in assemble(plan, details)['meals'][0]['ingredients']))

    def test_bad_source_still_stops(self):
        details = copy.deepcopy(self.details)
        details['recipes'][0]['source_url'] = 'javascript:alert(1)'
        with self.assertRaises(ValueError):
            assemble(self.plan, details)

    def test_unconvertible_cached_values_are_unknown(self):
        for amount, unit in [(None, '個'), ('1/0', 'g'), ('0', 'g'), ('-1', 'g'),
                             ('1〜2', '個'), ('nan', 'g'), ('2', '未対応')]:
            with self.subTest(amount=amount, unit=unit):
                detail = copy.deepcopy(self.details['recipes'][0])
                detail['ingredients'][0].update(amount=amount, unit=unit)
                rows = scale_ingredients(detail, 3)
                self.assertEqual(rows[0]['display'], '不明')
                self.assertIsNone(rows[0]['amount'])
                self.assertEqual(rows[1]['display'], '小さじ1')
        for servings in [None, '2', '2〜4', 0, -1, True]:
            with self.subTest(servings=servings):
                detail = copy.deepcopy(self.details['recipes'][0])
                detail['source_servings'] = servings
                self.assertTrue(all(i['display'] == '不明' for i in scale_ingredients(detail, 3)))
        with self.assertRaises(ValueError):
            scale_ingredients(self.details['recipes'][0], 0)

    def test_known_plus_unknown_html_markdown_and_json(self):
        plan, details = copy.deepcopy(self.plan), copy.deepcopy(self.details)
        for index, detail in enumerate(details['recipes']):
            detail['source_servings'] = 3
            detail['ingredients'] = [dict(name='玉ねぎ', amount='1' if index == 0 else None, unit='個')]
        data = assemble(plan, details)
        onion = data['shopping'][0]
        self.assertEqual(onion['display'], '1個＋不明')
        self.assertEqual(onion['quantities'], {'個': '1'})
        self.assertEqual(len(onion['as_needed']), 3)
        self.assertFalse(data['source_quantities_verified'])
        html = render_html(data, (BASE / 'templates/weekly.html').read_text(encoding='utf-8'))
        self.assertIn('玉ねぎ：1個＋不明', html)
        self.assertIn('玉ねぎ：不明', html)
        self.assertIn('玉ねぎ：1個＋不明', render_markdown(data))
        self.assertEqual(html.count('楽天レシピで作り方を見る'), 4)
        self.assertIn('data-key=', html)

    def test_no_ingredient_names_still_displays_source_and_unknown(self):
        data = assemble(self.plan, {'recipes': []})
        html = render_html(data, (BASE / 'templates/weekly.html').read_text(encoding='utf-8'))
        self.assertEqual(html.count('材料・分量：不明'), 4)
        self.assertEqual(html.count('元レシピは人数不明'), 4)
        self.assertNotIn('None', html)
        self.assertFalse(data['source_quantities_verified'])

    def test_html_escapes_titles(self):
        data = assemble(self.plan, self.details)
        data['meals'][0]['title'] = '<script>bad()</script>'
        html = render_html(data, (BASE / 'templates/weekly.html').read_text(encoding='utf-8'))
        self.assertNotIn('<script>bad()</script>', html)
        self.assertIn('&lt;script&gt;bad()', html)
        self.assertEqual(html.count('<article>'), 4)
        self.assertIn('noindex, nofollow', html)

    def test_invalid_input_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            (root / 'output').mkdir()
            (root / 'data/weekly_plan.json').write_text(json.dumps(self.plan), encoding='utf-8')
            bad = copy.deepcopy(self.details)
            bad['recipes'][0]['source_url'] = 'https://example.invalid/'
            (root / 'data/recipe_details.json').write_text(json.dumps(bad), encoding='utf-8')
            output = root / 'output/weekly-preview.html'
            output.write_text('previous', encoding='utf-8')
            with self.assertRaises(ValueError):
                generate_page(root)
            self.assertEqual(output.read_text(encoding='utf-8'), 'previous')


if __name__ == '__main__':
    unittest.main()

import copy
from datetime import timedelta
import unittest
import json
from unittest.mock import patch
import update_dinner as update
import test_update_dinner as fixtures
from plan_week import build_plan, record_plan
from test_plan_week import candidate, entry, report, DAYS, MONDAY


def recipe(key, category, score=3):
    return dict(candidate(key, '鶏肉', score), categories=[category])


class CategoryRedrawTests(unittest.TestCase):
    def previous(self):
        return [dict(entry(MONDAY + timedelta(days=d), 'previous-' + str(d)), categories=['さば', '厚揚げ'])
                for d in [0, 1, 2, 4]]

    def test_alternating_categories_never_revive_previous_sets_or_families(self):
        first = recipe('first', 'A')
        first['title'] = 'しょうが焼き'
        history = [dict(entry(MONDAY, 'first'), title='しょうが焼き', categories=['A'])]
        past = recipe('past', 'B')
        history += [dict(entry(MONDAY - timedelta(days=7), 'past'), categories=['B'])]
        previous = {'week_start': MONDAY.isoformat(), 'meals': [{'recipe': first}]}
        second = build_plan(report(first, past), history, ['月'], MONDAY, previous_plan=previous)
        self.assertTrue(second['reused_history'])
        history = record_plan(history, second)
        alias = dict(recipe('alias', 'A'), title='生姜焼き')
        third = build_plan(report(first, past, alias, recipe('fresh','A')), history, ['月'], MONDAY, previous_plan=second)
        self.assertEqual(third['meals'][0]['recipe']['recipe_id'], 'fresh')
        self.assertIn('first', {r['recipe_id'] for r in third['redraw_seen_recipes']})
        bad = copy.deepcopy(third)
        bad['meals'][0]['recipe'] = alias
        with self.assertRaises(ValueError):
            record_plan(history, bad)
        new_week = build_plan(report(first), history, ['月'], MONDAY + timedelta(weeks=5), previous_plan=third)
        self.assertEqual(new_week['status'], 'draft_ready')
        self.assertEqual(new_week['redraw_seen_recipes'], [])

    def test_previous_two_runs_are_imported_from_legacy_plan_ids(self):
        history = [dict(entry(MONDAY, 'B'), categories=['B'])]
        previous = {'week_start': MONDAY.isoformat(), 'redraw_previous_recipe_ids': ['A'],
                    'meals': [{'recipe': recipe('B','B')}]}
        result = build_plan(report(recipe('A','A'),recipe('B','B')),history,['月'],MONDAY,previous_plan=previous)
        self.assertEqual(result['status'], 'insufficient_candidates')

    def test_extra_category_selection_avoids_recent_fetches_and_previous_meals(self):
        previous = {'redraw_fetched_categories': list(update.REDRAW_CATEGORIES[:6]),
                    'meals': [{'recipe': recipe('a','鮭')}]}
        first = update.redraw_categories(previous)
        second = update.redraw_categories(previous, first)
        self.assertEqual(len(first), 6)
        self.assertNotIn('鮭', first + second)
        self.assertFalse(set(first) & set(second))
        self.assertFalse(set(first) & set(update.REDRAW_CATEGORIES[:6]))

    def test_all_previous_categories_are_excluded_even_for_different_ids(self):
        bad = [recipe('fish', 'さば', 999), recipe('tofu', '厚揚げ', 999),
               dict(recipe('mixed', '豚肉', 999), categories=['豚肉', 'さば'])]
        good = [recipe(str(i), '鶏むね肉') for i in range(4)]
        plan = build_plan(report(*bad, *good), self.previous(), DAYS, MONDAY)
        self.assertEqual(plan['status'], 'draft_ready')
        self.assertEqual({m['recipe']['recipe_id'] for m in plan['meals']}, {'0','1','2','3'})
        self.assertFalse(plan['reused_history'])
        self.assertEqual(set(plan['redraw_previous_categories']), {'さば','厚揚げ'})

    def test_uses_past_recipes_only_when_needed_and_records_categories(self):
        history = self.previous() + [entry(MONDAY - timedelta(days=7), 'past')]
        original = copy.deepcopy(history)
        good = [recipe(str(i), '鶏むね肉') for i in range(3)]
        data = report(*good, recipe('past', '豚薄切り肉'))
        plan = build_plan(data, history, DAYS, MONDAY)
        self.assertEqual(plan['status'], 'draft_ready')
        self.assertTrue(plan['reused_history'])
        self.assertIn('past', {m['recipe']['recipe_id'] for m in plan['meals']})
        saved = record_plan(history, plan)
        self.assertEqual(history, original)
        self.assertIn(history[-1], saved)
        self.assertEqual(len(saved), 5)
        self.assertTrue(all(r.get('categories') for r in saved if r['date'] >= MONDAY.isoformat()))
        self.assertEqual(record_plan(saved, plan), saved)
        fresh = build_plan(report(*good, recipe('fresh', '豚肉'), recipe('past','豚肉',999)), history, DAYS, MONDAY)
        self.assertFalse(fresh['reused_history'])
        self.assertNotIn('past', {m['recipe']['recipe_id'] for m in fresh['meals']})

    def test_legacy_history_gets_categories_from_saved_plan_by_id(self):
        history = self.previous()
        meals = [{'recipe': dict(r)} for r in history]
        for r in history:
            del r['categories']
        previous_plan = {'week_start': MONDAY.isoformat(), 'meals': meals}
        data = report(recipe('bad','さば',999), *(recipe(str(i),'鶏むね肉') for i in range(4)))
        plan = build_plan(data, history, DAYS, MONDAY, previous_plan=previous_plan)
        self.assertEqual(plan['status'], 'draft_ready')
        self.assertNotIn('bad', {m['recipe']['recipe_id'] for m in plan['meals']})

    def test_other_week_keeps_four_week_exclusion_without_category_exclusion(self):
        history = self.previous()
        data = report(*(recipe(str(i),'さば') for i in range(4)))
        plan = build_plan(data, history, DAYS, MONDAY + timedelta(weeks=1))
        self.assertEqual(plan['status'], 'draft_ready')
        self.assertEqual(plan['redraw_previous_categories'], [])
        blocked = build_plan(report(recipe('previous-0','別カテゴリ')), history, ['月'], MONDAY + timedelta(weeks=1))
        self.assertEqual(blocked['status'], 'insufficient_candidates')
        self.assertFalse(blocked['reused_history'])

    def test_fallback_does_not_relax_categories_or_seafood_limit(self):
        history = self.previous() + [entry(MONDAY - timedelta(days=7), 'past')]
        same = build_plan(report(recipe('past','さば')), history, ['月'], MONDAY)
        self.assertEqual(same['status'], 'insufficient_candidates')
        with self.assertRaises(ValueError):
            record_plan(history, same)
        fish = [dict(recipe(str(i),'鮭'), ingredient_names=['鮭']) for i in range(4)]
        plan = build_plan(report(*fish), history, DAYS, MONDAY)
        self.assertEqual(plan['status'], 'insufficient_candidates')
        self.assertEqual(sum(m['recipe'] is not None for m in plan['meals']), 1)

    def test_record_rejects_same_category_even_in_reuse_mode(self):
        history = self.previous()
        plan = build_plan(report(*(recipe(str(i),'鶏むね肉') for i in range(4))), history, DAYS, MONDAY)
        plan['reused_history'] = True
        plan['meals'][0]['recipe']['categories'] = ['さば']
        with self.assertRaisesRegex(ValueError, 'カテゴリ'):
            record_plan(history, plan)


class CategoryUpdateTests(unittest.TestCase):
    setUp = fixtures.UpdateTests.setUp
    tearDown = fixtures.UpdateTests.tearDown
    write = fixtures.UpdateTests.write

    def test_prepare_expands_categories_then_reuses_old_recipes(self):
        for meal in self.plan['meals']:
            meal['recipe']['categories'] = ['魚']
        self.write('data/weekly_plan.json', self.plan)
        recipes = [dict(self.plan['meals'][0]['recipe'], recipe_id='past-' + str(i),
                        title='肉料理' + str(i), categories=['鶏むね肉']) for i in range(4)]
        history = self.history + [dict(entry(MONDAY - timedelta(days=7-i), r['recipe_id']), title=r['title'])
                                  for i,r in enumerate(recipes)]
        self.write('history.json', history)
        initial = {'fetched_at': '2026-09-27', 'recipes': recipes[:2]}
        expanded = dict(initial, recipes=recipes)
        with patch.object(update, 'collect', side_effect=[initial, expanded]) as collect, \
                patch.object(update, 'fetch_detail', side_effect=ValueError('分量不明')), \
                patch.object(update.time, 'sleep'):
            artifacts,_ = update.prepare(self.base, self.base/'work', MONDAY, MONDAY)
        self.assertEqual(collect.call_count, 2)
        plan = json.loads(artifacts['data/weekly_plan.json'])
        self.assertTrue(plan['reused_history'])
        self.assertEqual(plan['redraw_previous_categories'], ['魚'])
        self.assertEqual({m['recipe']['recipe_id'] for m in plan['meals']}, {r['recipe_id'] for r in recipes})
        self.assertEqual(update.read_json(self.base/'history.json'), history)
        self.assertEqual((self.base/'output/index.html').read_text(), 'previous')


if __name__ == '__main__':
    unittest.main()

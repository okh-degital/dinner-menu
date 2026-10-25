import copy
from datetime import date, timedelta
import unittest

from plan_week import build_plan, record_plan
from test_plan_week import candidate, entry, report, DAYS

START = date(2026, 10, 12)
CHICKEN_ID = '1510019632'
CHICKEN_TITLE = '★鶏ムネ肉の柔らか甘辛チキン★'


class FourWeekHistoryTests(unittest.TestCase):
    def test_actual_chicken_id_blocked_every_day_even_with_changed_title(self):
        history = [dict(entry(date(2026, 9, 30), CHICKEN_ID), title=CHICKEN_TITLE)]
        original = copy.deepcopy(history)
        for weekday in DAYS:
            with self.subTest(weekday=weekday):
                chicken = dict(candidate(CHICKEN_ID, '鶏むね肉', 999), title='別表記')
                plan = build_plan(report(chicken, candidate('safe', '鶏むね肉')), history, [weekday], START)
                self.assertEqual(plan['meals'][0]['recipe']['recipe_id'], 'safe')
                self.assertEqual(plan['history_exclusions'][0]['recipe_ids'], [CHICKEN_ID])
                self.assertEqual(plan['history_window_days'], 28)
                self.assertEqual(plan['history_window_start'], '2026-09-14')
                self.assertEqual(plan['history_window_end'], '2026-10-11')
                record_plan(history, plan)
        self.assertEqual(history, original)

    def test_inclusive_boundaries_and_current_future_weeks(self):
        for offset, blocked in [(-29, False), (-28, True), (-1, True), (0, True), (6, True), (7, False)]:
            for weekday in DAYS:
                with self.subTest(offset=offset, weekday=weekday):
                    history = [entry(START + timedelta(days=offset), 'A')]
                    plan = build_plan(report(candidate('A')), history, [weekday], START)
                    self.assertEqual(plan['meals'][0]['recipe'] is None, blocked)
                    if not blocked:
                        record_plan(history, plan)

    def test_family_aliases_block_different_ids_across_month(self):
        old = dict(entry(date(2026, 9, 30), 'old'), title='天津丼')
        new = dict(candidate('new'), title='ふわトロ天津飯')
        plan = build_plan(report(new), [old], ['金'], START)
        self.assertEqual(plan['status'], 'insufficient_candidates')
        self.assertEqual(plan['history_exclusions'][0]['recipe_ids'], ['new'])
        with self.assertRaises(ValueError):
            record_plan([old], plan)

    def test_record_rechecks_id_and_family_for_each_day(self):
        for weekday in DAYS:
            for old, new in [
                (dict(entry(date(2026, 9, 30), CHICKEN_ID), title=CHICKEN_TITLE),
                 dict(candidate(CHICKEN_ID), title=CHICKEN_TITLE)),
                (dict(entry(date(2026, 9, 14), 'old'), title='しょうが焼き'),
                 dict(candidate('new'), title='生姜焼き')),
            ]:
                with self.subTest(weekday=weekday, old=old):
                    plan = build_plan(report(new), [], [weekday], START)
                    with self.assertRaisesRegex(ValueError, '直前4週間'):
                        record_plan([old], plan)

    def test_family_allowed_before_window_but_blocked_on_current_week_rerun(self):
        new = dict(candidate('new'), title='生姜焼き')
        for day in [date(2026, 9, 13), START, START + timedelta(days=6)]:
            with self.subTest(day=day):
                old = dict(entry(day, 'old'), title='しょうが焼き')
                plan = build_plan(report(new), [old], ['金'], START)
                if day >= START:
                    self.assertEqual(plan['status'], 'insufficient_candidates')
                    continue
                self.assertEqual(plan['status'], 'draft_ready')
                updated = record_plan([old], plan)
                self.assertEqual(len(updated), 2 if day < START else 1)
                self.assertEqual(record_plan(updated, plan), updated)

    def test_same_meat_different_menus_allowed_within_month_and_week(self):
        history = [dict(entry(date(2026, 10, 5), CHICKEN_ID), title=CHICKEN_TITLE)]
        recipes = [dict(candidate(str(i), '鶏むね肉'), title=title) for i, title in enumerate(
            ['チキン南蛮', '鶏肉の照り焼き', '鶏肉の唐揚げ', '蒸し鶏'])]
        plan = build_plan(report(*recipes), history, DAYS, START)
        self.assertEqual(plan['status'], 'draft_ready')
        self.assertEqual(len(record_plan(history, plan)), 5)

    def test_same_fish_allowed_next_week_but_not_twice_in_week(self):
        history = [dict(entry(date(2026, 10, 5), 'old'), title='サバの味噌煮')]
        recipes = [dict(candidate('a', 'サバ'), title='サバの塩焼き'),
                   dict(candidate('b', '鯖'), title='鯖の竜田揚げ')]
        plan = build_plan(report(*recipes), history, ['月', '金'], START)
        self.assertEqual(sum(m['recipe'] is not None for m in plan['meals']), 1)
        single = build_plan(report(recipes[0]), history, ['金'], START)
        self.assertEqual(single['status'], 'draft_ready')
        record_plan(history, single)

    def test_year_boundary_uses_four_full_weeks(self):
        start = date(2027, 1, 4)
        history = [entry(date(2026, 12, 7), 'blocked'), entry(date(2026, 12, 6), 'allowed')]
        plan = build_plan(report(candidate('blocked'), candidate('allowed')), history, ['金'], start)
        self.assertEqual(plan['meals'][0]['recipe']['recipe_id'], 'allowed')
        self.assertEqual(plan['history_window_end'], '2027-01-03')


if __name__ == '__main__':
    unittest.main()

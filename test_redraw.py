import copy
from datetime import date, datetime, timezone, timedelta
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import fetch_rakuten as api
import select_dinner
import update_dinner as update
from plan_week import build_plan, record_plan, seafood_species
from test_plan_week import MONDAY, DAYS, candidate, report, entry


class RedrawTests(unittest.TestCase):
    def test_repeated_week_changes_set_and_retains_other_weeks(self):
        data = report(*(candidate(str(i)) for i in range(6)))
        old = [entry(MONDAY - timedelta(days=14), 'old'), entry(MONDAY + timedelta(days=14), 'future')]
        history = old[:]
        previous = None
        for _ in range(8):
            plan = build_plan(data, history, DAYS, MONDAY)
            chosen = {m['recipe']['recipe_id'] for m in plan['meals']}
            self.assertNotEqual(chosen, previous)
            history = record_plan(history, plan)
            self.assertEqual(len(history), 6)
            self.assertTrue(all(row in history for row in old))
            previous = chosen

    def test_week_replacement_removes_old_offday_and_crosses_month(self):
        history = [entry(date(2026, 10, 1), 'obsolete'), entry(date(2026, 9, 27), 'keep')]
        plan = build_plan(report(candidate('new')), history, ['月'], MONDAY)
        self.assertEqual([r['recipe_id'] for r in record_plan(history, plan)], ['keep', 'new'])

    def test_aliases_and_seasonings(self):
        for name in ['サバ', 'さば', '鯖', 'ｻﾊﾞ', '真鯖', 'さば水煮缶']:
            self.assertEqual(seafood_species(candidate('x', name)), {'さば'})
        for name in ['鮭', 'サーモン', '塩さけ']:
            self.assertEqual(seafood_species(candidate('x', name)), {'さけ'})
        for name in ['かつおだし', '鰹節', '料理酒', 'カニカマ', 'オイスターソース']:
            self.assertEqual(seafood_species(candidate('x', name)), set())
        self.assertEqual(seafood_species({'title': 'タコライス'}), set())
        self.assertEqual(seafood_species(candidate('x', 'エビ・イカ')), {'えび', 'いか'})

    def test_fish_aliases_blocked_but_meat_allowed(self):
        data = report(candidate('a', 'サバ', 100), candidate('b', '鯖', 100),
                      candidate('c', 'さば', 100), candidate('d', '鮭'),
                      candidate('e', '豚肉'), candidate('f', '豚肉'))
        plan = build_plan(data, [], DAYS, MONDAY)
        self.assertEqual(plan['status'], 'draft_ready')
        self.assertEqual(sum('さば' in seafood_species(m['recipe']) for m in plan['meals']), 1)
        self.assertEqual(sum(m['recipe']['ingredient_names'] == ['豚肉'] for m in plan['meals']), 2)
        record_plan([], plan)
        bad = copy.deepcopy(plan)
        bad['meals'][0]['recipe'] = candidate('bad1', 'サバ')
        bad['meals'][1]['recipe'] = candidate('bad2', '鯖')
        with self.assertRaisesRegex(ValueError, '魚介'):
            record_plan([], bad)

    def test_only_same_species_is_shortage_not_relaxed(self):
        plan = build_plan(report(candidate('a', '鯖'), candidate('b', 'サバ')), [], ['月', '火'], MONDAY)
        self.assertEqual(plan['status'], 'insufficient_candidates')

    def test_sunday_default_targets_following_monday(self):
        class Sunday(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 4, 10, tzinfo=timezone(timedelta(hours=9)))
        from contextlib import nullcontext
        with patch.object(update, 'datetime', Sunday), patch.object(update, 'update_lock', return_value=nullcontext()), \
                patch.object(update, 'prepare', side_effect=ValueError('stop')) as prepare, patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(update.main([]), 1)
        self.assertEqual(prepare.call_args.args[2], date(2026, 10, 5))


class ApiErrorTests(unittest.TestCase):
    def test_ranking_403_with_ip_no_credentials_in_message(self):
        error = HTTPError('https://example.invalid/?secret=hidden', 403, 'hidden', {}, None)
        with patch.object(api, 'urlopen', side_effect=[error, io.BytesIO(b'8.8.8.8')]) as request:
            with self.assertRaisesRegex(ValueError, 'IPアドレスを変更してください。') as caught:
                api.fetch_ranking('secret-app', 'secret-key')
        self.assertIn('8.8.8.8', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(request.call_args.args, ('https://api.ipify.org',))

    def test_category_403_and_ip_failure(self):
        with patch.object(select_dinner, 'load_credentials', return_value=('app', 'key')), \
                patch.object(select_dinner, 'urlopen', side_effect=HTTPError('url', 403, '', {}, None)), \
                patch.object(api, 'urlopen', side_effect=URLError('offline')):
            with self.assertRaisesRegex(ValueError, 'IPアドレスを変更してください。'):
                select_dinner.collect(None)

    def test_invalid_ip_and_other_status(self):
        with patch.object(api, 'urlopen', return_value=io.BytesIO(b'not-an-ip')):
            self.assertIn('IPアドレスを変更してください。', api.api_error_message(403))
        with patch.object(api, 'urlopen') as request:
            self.assertIn('HTTP 429', api.api_error_message(429))
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()

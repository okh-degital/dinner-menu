import unittest
from datetime import date, timedelta

from plan_week import build_plan, record_plan
from select_dinner import dish_family, is_rice_bowl

MONDAY = date(2026, 9, 28)
DAYS = ["月", "火", "水", "金"]


def candidate(recipe_id, ingredient="豚肉", score=3, status="candidate"):
    return {"source": "rakuten_recipe", "recipe_id": recipe_id, "title": recipe_id,
            "ingredient_names": [ingredient], "score": score, "status": status}


def report(*recipes):
    return {"source_fetched_at": "2026-09-27T09:00:00+00:00", "recipes": list(recipes)}


def entry(day, recipe_id):
    return {"date": day.isoformat(), "source": "rakuten_recipe", "recipe_id": recipe_id,
            "title": recipe_id, "status": "planned"}


class WeekPlanTests(unittest.TestCase):
    def test_different_bowls_and_taco_recipes_share_one_weekly_slot(self):
        bowls = [dict(candidate(str(i), score=50), title=title) for i, title in enumerate(
            ["沖縄の味★タコライス", "簡単タコライス", "豚丼", "天津飯"])]
        mains = [candidate("fish", "鮭"), candidate("tofu", "豆腐"), candidate("chicken", "鶏肉")]
        plan = build_plan(report(*bowls, *mains), [], DAYS, MONDAY)
        self.assertEqual(plan["status"], "draft_ready")
        self.assertEqual(sum(is_rice_bowl(m["recipe"]) for m in plan["meals"]), 1)

    def test_bowl_only_shortage_never_relaxes_limit(self):
        data = report(*(dict(candidate(str(i)), title=t) for i, t in enumerate(["牛丼", "豚丼", "天津飯", "タコライス"])))
        plan = build_plan(data, [], DAYS, MONDAY)
        self.assertEqual(plan["status"], "insufficient_candidates")
        self.assertEqual(sum(m["recipe"] is not None for m in plan["meals"]), 1)
        with self.assertRaises(ValueError):
            record_plan([], plan)

    def test_taco_month_limit_uses_history_titles_across_recipe_ids(self):
        taco = dict(candidate("new", score=50), title="簡単タコライス")
        regular = candidate("regular")
        history = [dict(entry(date(2026, 9, 1), "old"), title="沖縄の味★タコライス")]
        plan = build_plan(report(taco, regular), history, ["月"], MONDAY)
        self.assertEqual(plan["meals"][0]["recipe"]["recipe_id"], "regular")
        # 次の暦月には別IDのタコライスを選べる。
        next_month = build_plan(report(taco, regular), history, ["月"], date(2026, 10, 5))
        self.assertEqual(next_month["meals"][0]["recipe"]["recipe_id"], "new")

    def test_month_boundary_uses_each_meal_date_and_keeps_weekly_limit(self):
        history = [dict(entry(date(2026, 9, 1), "old"), title="タコライス")]
        taco = dict(candidate("new"), title="タコライス")
        plan = build_plan(report(taco), history, ["水", "金"], MONDAY)
        self.assertIsNone(plan["meals"][0]["recipe"])
        self.assertEqual(plan["meals"][1]["recipe"]["recipe_id"], "new")
        no_history = build_plan(report(taco, dict(candidate("other"), title="沖縄タコライス")), [], ["水", "金"], MONDAY)
        self.assertEqual(sum(m["recipe"] is not None for m in no_history["meals"]), 1)

    def test_existing_off_day_bowl_and_future_monthly_plan_are_counted(self):
        regular = candidate("regular")
        bowl = dict(candidate("bowl", score=50), title="豚丼")
        history = [dict(entry(date(2026, 10, 1), "other"), title="天津丼")]
        self.assertEqual(build_plan(report(bowl, regular), history, ["月"], MONDAY)["meals"][0]["recipe"]["recipe_id"], "regular")
        taco = dict(candidate("taco", score=50), title="タコライス")
        future = [dict(entry(date(2026, 10, 20), "reserved"), title="タコライス")]
        self.assertEqual(build_plan(report(taco, regular), future, ["月"], date(2026, 10, 5))["meals"][0]["recipe"]["recipe_id"], "regular")

    def test_record_rechecks_monthly_limit_and_remains_idempotent(self):
        taco = dict(candidate("new"), title="タコライス")
        plan = build_plan(report(taco), [], ["月"], MONDAY)
        updated = record_plan([], plan)
        self.assertEqual(record_plan(updated, plan), updated)
        history = [dict(entry(date(2026, 9, 1), "other"), title="沖縄タコライス")]
        with self.assertRaises(ValueError):
            record_plan(history, plan)
        # 保存後に混入した別ID・別名の丼物も記録させない。
        plan["meals"].append({"date": "2026-09-29", "weekday": "火", "recipe": dict(candidate("bowl"), title="豚丼")})
        with self.assertRaises(ValueError):
            record_plan([], plan)

    def test_dish_aliases_do_not_merge_different_dishes(self):
        self.assertEqual(dish_family({"title": "天津丼"}), dish_family({"title": "ふわトロ天津飯"}))
        self.assertEqual(dish_family({"title": "ﾀｺ ﾗｲｽ"}), "タコライス")
        self.assertNotEqual(dish_family({"title": "牛丼"}), dish_family({"title": "豚丼"}))

    def test_four_dates_unique_ids_and_variety(self):
        data = report(candidate("A"), candidate("A"), candidate("B"), candidate("C", "豆腐"),
                      candidate("D", "卵"), candidate("E", "エビ"), candidate("held", status="hold"))
        plan = build_plan(data, [], DAYS, MONDAY)
        self.assertEqual(plan["status"], "draft_ready")
        self.assertEqual([m["date"] for m in plan["meals"]],
                         ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-02"])
        self.assertEqual(len({m["recipe"]["recipe_id"] for m in plan["meals"]}), 4)
        self.assertEqual(len({m["group"] for m in plan["meals"]}), 4)
        self.assertNotIn("held", [m["recipe"]["recipe_id"] for m in plan["meals"]])

    def test_14_days_inclusive_and_15_days_expired(self):
        history = [entry(MONDAY - timedelta(days=14), "A"), entry(MONDAY - timedelta(days=15), "B")]
        plan = build_plan(report(candidate("A"), candidate("B")), history, ["月"], MONDAY)
        self.assertEqual(plan["meals"][0]["recipe"]["recipe_id"], "B")
        self.assertEqual(plan["history_exclusions"][0]["recipe_ids"], ["A"])

    def test_window_moves_with_meal_date(self):
        history = [entry(MONDAY - timedelta(days=13), "A")]
        plan = build_plan(report(candidate("A")), history, ["月", "金"], MONDAY)
        self.assertIsNone(plan["meals"][0]["recipe"])
        self.assertEqual(plan["meals"][1]["recipe"]["recipe_id"], "A")

    def test_assignment_does_not_waste_scarce_eligible_recipe(self):
        history = [entry(MONDAY - timedelta(days=14), "B")]
        plan = build_plan(report(candidate("A"), candidate("B")), history, ["月", "火"], MONDAY)
        self.assertEqual(plan["status"], "draft_ready")
        self.assertEqual([m["recipe"]["recipe_id"] for m in plan["meals"]], ["A", "B"])

    def test_shortage_never_reuses_or_records(self):
        plan = build_plan(report(candidate("A")), [], DAYS, MONDAY)
        self.assertEqual(plan["status"], "insufficient_candidates")
        self.assertEqual(sum(m["recipe"] is not None for m in plan["meals"]), 1)
        with self.assertRaises(ValueError):
            record_plan([], plan)

    def test_record_is_idempotent_and_draft_does_not_mutate_history(self):
        history = []
        plan = build_plan(report(*(candidate(str(i)) for i in range(4))), history, DAYS, MONDAY)
        self.assertEqual(history, [])
        updated = record_plan(history, plan)
        self.assertEqual(len(updated), 4)
        self.assertEqual(record_plan(updated, plan), updated)
        self.assertEqual(history, [])

    def test_changed_history_blocks_recording(self):
        plan = build_plan(report(candidate("A")), [], ["月"], MONDAY)
        for history in ([entry(MONDAY, "B")], [entry(MONDAY - timedelta(days=1), "A")]):
            with self.assertRaises(ValueError):
                record_plan(history, plan)

    def test_invalid_history_week_and_duplicate_weekdays(self):
        data = report(candidate("A"))
        with self.assertRaises(ValueError):
            build_plan(data, [{"date": "broken"}], DAYS, MONDAY)
        with self.assertRaises(ValueError):
            build_plan(data, [], DAYS, MONDAY + timedelta(days=1))
        with self.assertRaises(ValueError):
            build_plan(data, [], ["月", "月"], MONDAY)
        with self.assertRaises(ValueError):
            build_plan(data, [entry(MONDAY, "A")], DAYS, MONDAY)


if __name__ == "__main__":
    unittest.main()

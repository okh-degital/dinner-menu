import unittest
from datetime import date, timedelta

from plan_week import build_plan, record_plan

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

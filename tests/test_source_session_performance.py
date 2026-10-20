"""Восстановление старого сеанса сохраняет принадлежность тегов группе."""

import copy
import unittest

import test_source_session


class SourceSessionRecoveryTests(unittest.TestCase):
    def test_same_members_in_unrelated_results_do_not_replace_group_metadata(self):
        fixture = test_source_session.SourceSessionTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app()
        fixture.add_group(app)
        snapshot = app._build_source_session_snapshot()
        snapshot["items"][-1]["group"] = None
        accepted = {
            "kind": "m4b", "file_ids": ("01.txt", "02.txt"),
            "item_id": "m4b_plan:my-group", "group_name": "Моя часть",
            "name_template": "Книга {part}", "metadata_overrides": {"artist": "Автор группы"},
        }
        unrelated = dict(accepted, item_id="other", group_name="Другая группа",
                         metadata_overrides={"artist": "Посторонний автор"})
        other_members = dict(accepted, file_ids=("03.txt",), metadata_overrides={"artist": "Другая глава"})
        snapshot["completed_m4b"] = {"unrelated": unrelated, "accepted": accepted, "other": other_members}
        original = copy.deepcopy(snapshot)
        app._restore_source_session_tree(snapshot)
        restored = app._source_plan_groups["m4b_plan:my-group"]
        self.assertEqual(restored["file_ids"], ("01.txt", "02.txt"))
        self.assertEqual(restored["metadata_overrides"], {"artist": "Автор группы"})
        self.assertEqual(restored["name_template"], "Книга {part}")
        self.assertEqual(snapshot, original)


if __name__ == "__main__":
    unittest.main()

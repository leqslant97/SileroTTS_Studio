"""Возобновление ручного дерева синтеза после обновления и перезапуска."""

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_NAME = "silero_tts_studio_under_test"
if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, Path(__file__).resolve().parents[1] / "SileroTTS_Studio.py"
    )
    studio = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = studio
    spec.loader.exec_module(studio)
else:
    studio = sys.modules[MODULE_NAME]


class MemoryTree:
    def __init__(self):
        self.nodes = {}
        self.children = {"": []}

    def get_children(self, parent=""):
        return tuple(self.children.get(parent, ()))

    def insert(self, parent, position, *, iid, **kwargs):
        if iid in self.nodes:
            raise ValueError("повторный идентификатор")
        self.nodes[iid] = dict(kwargs, parent=parent)
        self.children.setdefault(parent, []).append(iid)
        self.children[iid] = []
        return iid

    def delete(self, *items):
        for item in items:
            self.delete(*tuple(self.children[item]))
            parent = self.nodes.pop(item)["parent"]
            self.children[parent].remove(item)
            del self.children[item]

    def item(self, item, option=None, **kwargs):
        self.nodes[item].update(kwargs)
        return self.nodes[item].get(option) if option else self.nodes[item]

    def move(self, item, parent, _position):
        self.children[self.nodes[item]["parent"]].remove(item)
        self.nodes[item]["parent"] = parent
        self.children.setdefault(parent, []).append(item)

    def exists(self, item):
        return item in self.nodes

    def parent(self, item):
        return self.nodes[item]["parent"]

    def selection(self):
        return ()

    def state(self, _state):
        pass

    def configure(self, **_kwargs):
        pass

    def heading(self, *_args, **_kwargs):
        pass


class SourceSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "texts"
        self.source.mkdir()
        for name in ("01.txt", "02.txt", "03.txt"):
            (self.source / name).write_text("Текст " + name, encoding="utf-8")
        self.session_path = self.root / "source_session.json"
        patcher = mock.patch.object(studio, "SOURCE_SESSION_FILE", self.session_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(studio.tk, "BooleanVar")
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, config_updates=None):
        app = object.__new__(studio.TTSApp)
        app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        app.config.update(input_dir=str(self.source), cache_dir=str(self.root / "cache"))
        app.config.update(config_updates or {})
        app.root = mock.Mock()
        app.tree = MemoryTree()
        app.include_subdirs_var = mock.Mock()
        app.include_subdirs_var.get.return_value = False
        app.save_settings = mock.Mock()
        app.ensure_dirs = mock.Mock()
        app._set_status_label = mock.Mock()
        app._show_info = mock.Mock()
        app._show_warning = mock.Mock()
        app._show_error = mock.Mock()
        app._ask_yes_no = mock.Mock(return_value=True)
        for name in ("lbl_total_pct", "lbl_file_pct", "btn_go_current", "lbl_current_text"):
            setattr(app, name, mock.Mock())
        app.total_progress = {}
        app.file_progress = {}
        app.load_files()
        return app

    def add_group(self, app):
        app.tree.insert("", "end", iid="m4b_plan:my-group", text="Моя часть")
        for file_id in ("01.txt", "02.txt"):
            app.tree.move(file_id, "m4b_plan:my-group", "end")
            app._source_parent_by_id[file_id] = "m4b_plan:my-group"
        app._source_group_ids.add("m4b_plan:my-group")
        app._source_plan_groups["m4b_plan:my-group"] = {
            "name": "Моя часть", "name_template": None,
            "file_ids": ("01.txt", "02.txt"),
            "metadata_overrides": {"album": "Другой альбом", "artist": "Автор"},
        }

    def record(self, *, kind="file"):
        target = studio.normalize_output_target({"format": "m4b" if kind == "m4b" else "mp3"})
        return {
            "kind": kind, "target_index": 0, "target": target,
            "path": self.root / ("book.m4b" if kind == "m4b" else "01.mp3"),
            "file_ids": ("01.txt", "02.txt") if kind == "m4b" else ("01.txt",),
            "source_paths": (self.source / "01.txt", self.source / "02.txt") if kind == "m4b" else (self.source / "01.txt",),
        }

    def output_records(self, app):
        return studio.plan_source_synthesis_target_paths(
            self.source, self.root / "outputs", app._source_tree_file_ids(),
            targets=(
                {"format": "mp3"},
                {"format": "opus", "assembly_mode": "merge"},
                {"format": "m4b"},
            ),
            path_by_id=app._source_path_by_id,
            source_m4b_groups=app._source_plan_groups,
            m4b_template="{book}",
        )

    def complete_outputs(self, app, records):
        app._remember_source_completed_regular_targets(app._source_plan_revision, records)
        app._remember_source_runtime_m4b_records(records)
        app._source_plan_dirty = False

    def group_all_files(self, app, *, name=None, metadata=None, reverse=False):
        group_id = "m4b_plan:temporary-id"
        file_ids = tuple(app._source_tree_file_ids())
        if reverse:
            file_ids = tuple(reversed(file_ids))
        app.tree.insert("", "end", iid=group_id, text=name or self.source.name)
        for file_id in file_ids:
            app.tree.move(file_id, group_id, "end")
            app._source_parent_by_id[file_id] = group_id
        app._source_group_ids.add(group_id)
        app._source_plan_groups[group_id] = {
            "name": name or self.source.name, "name_template": "{book}",
            "file_ids": file_ids, "estimated_duration": 123.0,
            "metadata_overrides": metadata or {},
        }

    def test_restart_and_refresh_keep_names_tags_order_and_completed_targets(self):
        first = self.app()
        self.add_group(first)
        first._source_plan_dirty = True
        record = self.record()
        first._remember_source_completed_regular_targets(first._source_plan_revision, [record])
        first._remember_source_runtime_m4b_records([self.record(kind="m4b")])
        first._save_source_session()
        original_order = first._source_tree_file_ids()
        second = self.app()
        self.assertEqual(second._source_tree_file_ids(), original_order)
        self.assertEqual(second._source_plan_groups, first._source_plan_groups)
        self.assertEqual(second._source_regular_rebuild_paths([record]), ())
        self.assertEqual(second._source_runtime_m4b_records, first._source_runtime_m4b_records)
        second.load_files()
        self.assertEqual(second._source_plan_groups, first._source_plan_groups)
        self.assertEqual(second._source_regular_rebuild_paths([record]), ())

    def plan_snapshot(self, app):
        app._source_plan_snapshot = {
            "input_dir": self.source,
            "paths": dict(app._source_path_by_id),
            "file_ids": tuple(app._source_tree_file_ids()),
        }
        app._source_plan_running = True
        app._source_plan_reload_pending = False

    def test_estimated_plan_uses_visible_order_not_path_dictionary_insertion(self):
        app = self.app()
        self.plan_snapshot(app)
        app._source_path_by_id = dict(reversed(tuple(app._source_path_by_id.items())))
        groups = (("01.txt", "02.txt"), ("03.txt",))

        app._finish_m4b_source_plan(groups, {}, template="Том {part}")

        app._show_warning.assert_not_called()
        app._show_error.assert_not_called()
        self.assertEqual(tuple(
            group["file_ids"] for group in app._source_plan_groups.values()
        ), groups)
        self.assertEqual(tuple(
            group["name"] for group in app._source_plan_groups.values()
        ), ("Том 1", "Том 2"))

    def test_reordered_tree_rejects_stale_estimate_without_losing_existing_group(self):
        app = self.app()
        self.add_group(app)
        group_id = "m4b_plan:my-group"
        original = copy.deepcopy(app._source_plan_groups[group_id])
        self.plan_snapshot(app)
        app.tree.move("01.txt", group_id, "end")

        app._finish_m4b_source_plan((("03.txt", "01.txt", "02.txt"),), {}, template="Новое имя")

        app._show_warning.assert_called_once()
        self.assertIn(group_id, app._source_plan_groups)
        self.assertEqual(app._source_plan_groups[group_id]["name"], original["name"])
        self.assertEqual(app._source_plan_groups[group_id]["metadata_overrides"], original["metadata_overrides"])
        self.assertEqual(app.tree.get_children(group_id), ("02.txt", "01.txt"))
        app._save_source_session()
        restored = self.app()
        self.assertEqual(restored._source_plan_groups[group_id]["name"], original["name"])
        self.assertEqual(restored._source_plan_groups[group_id]["metadata_overrides"], original["metadata_overrides"])

    def test_repeating_identical_estimate_preserves_completed_outputs(self):
        app = self.app()
        groups = (("01.txt", "02.txt"), ("03.txt",))
        app._finish_m4b_source_plan(groups, {}, template="Том {part}")
        app._check_source_session_changes()
        self.complete_outputs(app, self.output_records(app))
        old_group_ids = tuple(app._source_plan_groups)
        self.plan_snapshot(app)

        app._finish_m4b_source_plan(groups, {"01.txt": 20}, template="Том {part}")

        self.assertNotEqual(tuple(app._source_plan_groups), old_group_ids)
        records = self.output_records(app)
        self.assertEqual(app._source_regular_rebuild_paths(records), ())
        self.assertEqual(app._source_m4b_rebuild_paths(records), ())
        app._save_source_session()
        restored = self.app({"synthesis_m4b_template": "Том {part}"})
        restored_records = self.output_records(restored)
        self.assertEqual(restored._source_regular_rebuild_paths(restored_records), ())
        self.assertEqual(restored._source_m4b_rebuild_paths(restored_records), ())

    def test_changed_estimate_boundaries_rebuild_groups_but_preserve_chapters(self):
        app = self.app()
        app._finish_m4b_source_plan((("01.txt", "02.txt"), ("03.txt",)), {}, template="Том {part}")
        app._check_source_session_changes()
        self.complete_outputs(app, self.output_records(app))
        self.plan_snapshot(app)

        app._finish_m4b_source_plan((("01.txt", "02.txt", "03.txt"),), {}, template="Том {part}")

        records = self.output_records(app)
        self.assertEqual(app._source_regular_rebuild_paths(records), tuple(
            str(record["path"]) for record in records if record["kind"] == "group"
        ))
        self.assertEqual(app._source_m4b_rebuild_paths(records), tuple(
            str(record["path"]) for record in records if record["kind"] == "m4b"
        ))

    def test_missing_virtual_group_definition_recovers_visible_partition(self):
        for with_accepted_metadata in (False, True):
            with self.subTest(with_accepted_metadata=with_accepted_metadata):
                self.session_path.unlink(missing_ok=True)
                self.session_path.with_suffix(".json.bak").unlink(missing_ok=True)
                app = self.app()
                self.add_group(app)
                group_id = "m4b_plan:my-group"
                if with_accepted_metadata:
                    self.complete_outputs(app, self.output_records(app))
                snapshot = app._build_source_session_snapshot()
                damaged = next(node for node in snapshot["items"] if node["id"] == group_id)
                damaged["group"] = None
                self.session_path.write_text(
                    json.dumps(app._source_session_pack(snapshot), ensure_ascii=False), encoding="utf-8",
                )

                restored = self.app()

                self.assertIn(group_id, restored._source_plan_groups)
                group = restored._source_plan_groups[group_id]
                self.assertEqual(group["name"], "Моя часть")
                self.assertEqual(group["file_ids"], ("01.txt", "02.txt"))
                if with_accepted_metadata:
                    self.assertEqual(group["metadata_overrides"], {"album": "Другой альбом", "artist": "Автор"})
                planned = self.output_records(restored)
                self.assertTrue(any(
                    record["kind"] == "m4b" and record["file_ids"] == ("01.txt", "02.txt")
                    for record in planned
                ))
                self.assertFalse(any(
                    record["kind"] == "m4b" and len(record["file_ids"]) == 3 for record in planned
                ))
                self.session_path.unlink(missing_ok=True)

    def test_start_rejects_orphaned_virtual_group_instead_of_merging_entire_book(self):
        app = self.app({"synthesis_targets": [{"format": "m4b"}]})
        self.add_group(app)
        app._source_plan_groups.clear()
        app.batch_processor = None
        app.direct_processor = None
        app._warn_if_cache_busy_for_synthesis = mock.Mock(return_value=False)
        app._validate_api_steps_ui = mock.Mock(return_value=True)
        app._validate_book_output_profile = mock.Mock(return_value=True)

        with mock.patch.object(studio, "TTSProcessor") as processor:
            app.start_processing()

        processor.assert_not_called()
        app._show_error.assert_called_once()
        message = app._show_error.call_args.args[1]
        self.assertIn("Обновить папку", message)
        self.assertIn("Объединение всей очереди", message)
        self.assertEqual(app.tree.get_children("m4b_plan:my-group"), ("01.txt", "02.txt"))

    def test_refresh_adds_new_files_removes_missing_and_keeps_remaining_group(self):
        app = self.app()
        self.add_group(app)
        app._source_plan_dirty = False
        app._remember_source_runtime_m4b_records([self.record(kind="m4b")])
        (self.source / "01.txt").unlink()
        (self.source / "04.txt").write_text("Новая глава", encoding="utf-8")
        app.load_files()
        self.assertEqual(app._source_plan_groups["m4b_plan:my-group"]["file_ids"], ("02.txt",))
        self.assertEqual(app._source_plan_groups["m4b_plan:my-group"]["name"], "Моя часть")
        self.assertIn("04.txt", app._source_tree_file_ids())
        self.assertNotIn("01.txt", app._source_tree_file_ids())
        self.assertTrue(app._source_plan_dirty)
        self.assertIsNone(app._source_runtime_m4b_records)

    def test_changed_text_invalidates_results_even_without_refresh(self):
        app = self.app()
        record = self.record()
        app._source_plan_dirty = True
        app._remember_source_completed_regular_targets(app._source_plan_revision, [record])
        app._source_plan_dirty = False
        (self.source / "01.txt").write_text("Изменённый текст главы", encoding="utf-8")
        app._check_source_session_changes()
        self.assertTrue(app._source_plan_dirty)
        self.assertEqual(app._source_regular_rebuild_paths([record]), (str(record["path"]),))

    def test_limiter_and_token_do_not_invalidate_speech_but_pauses_do(self):
        app = self.app()
        baseline = app._source_session_settings_signature()
        app.config.update(api_token="не сохранять", api_max_requests=50, ui_font_size=18)
        self.assertEqual(app._source_session_settings_signature(), baseline)
        app._save_source_session()
        self.assertNotIn("не сохранять", self.session_path.read_text(encoding="utf-8"))
        app.config["pause_sentence"] = 730
        app._check_source_session_changes()
        self.assertTrue(app._source_plan_dirty)

    def test_reset_confirmation_and_explicitly_excluded_files(self):
        app = self.app()
        self.add_group(app)
        app.tree.delete("03.txt")
        app._source_path_by_id.pop("03.txt")
        app._save_source_session()
        app.load_files()
        self.assertNotIn("03.txt", app._source_path_by_id)
        app._ask_yes_no.return_value = False
        app.reset_source_m4b_groups()
        self.assertIn("m4b_plan:my-group", app._source_plan_groups)
        app._ask_yes_no.return_value = True
        app.reset_source_m4b_groups()
        self.assertEqual(app._source_plan_groups, {})
        self.assertEqual(app._source_tree_file_ids(), ["01.txt", "02.txt", "03.txt"])
        self.assertTrue(app._source_plan_dirty)

    def test_reset_preserves_unchanged_completed_outputs_after_restart(self):
        app = self.app()
        self.group_all_files(app)
        completed = self.output_records(app)
        self.complete_outputs(app, completed)

        app.reset_source_m4b_groups()

        fresh = self.output_records(app)
        self.assertEqual(app._source_plan_groups, {})
        self.assertIsNone(app._source_runtime_m4b_records)
        self.assertEqual(app._source_regular_rebuild_paths(fresh), ())
        self.assertEqual(app._source_m4b_rebuild_paths(fresh), ())
        self.assertTrue(app._save_source_session())
        restored = self.app()
        self.assertIsNone(restored._source_runtime_m4b_records)
        self.assertEqual(restored._source_regular_rebuild_paths(self.output_records(restored)), ())
        self.assertEqual(restored._source_m4b_rebuild_paths(self.output_records(restored)), ())

    def test_reset_changed_boundaries_rebuilds_groups_but_keeps_individual_outputs(self):
        app = self.app()
        self.add_group(app)
        app._source_plan_groups["m4b_plan:my-group"].update(
            name=self.source.name, metadata_overrides={},
        )
        completed = self.output_records(app)
        self.complete_outputs(app, completed)

        app.reset_source_m4b_groups()

        fresh = self.output_records(app)
        self.assertEqual(app._reuse_source_runtime_m4b_records(
            fresh, app._source_tree_file_ids(), app._source_path_by_id,
        ), fresh)
        self.assertEqual(app._source_regular_rebuild_paths(fresh), tuple(
            str(record["path"]) for record in fresh if record["kind"] == "group"
        ))
        self.assertEqual(app._source_m4b_rebuild_paths(fresh), tuple(
            str(record["path"]) for record in fresh if record["kind"] == "m4b"
        ))
        app._save_source_session()
        restored = self.app()
        self.assertEqual(restored._reuse_source_runtime_m4b_records(
            fresh, restored._source_tree_file_ids(), restored._source_path_by_id,
        ), fresh)

    def test_reset_after_published_runtime_plan_keeps_individual_completions(self):
        app = self.app()
        completed = self.output_records(app)
        self.complete_outputs(app, completed)
        m4b_records = tuple(record for record in completed if record["kind"] == "m4b")
        app._stage_source_m4b_ui_plan(
            m4b_records, {file_id: 1.0 for file_id in app._source_tree_file_ids()},
            app._source_tree_file_ids(), app._source_path_by_id,
        )

        self.assertTrue(app._apply_pending_source_m4b_ui_plan())
        self.assertTrue(app._source_plan_groups)
        app.reset_source_m4b_groups()

        fresh = self.output_records(app)
        self.assertEqual(app._source_regular_rebuild_paths(fresh), ())
        self.assertEqual(app._source_m4b_rebuild_paths(fresh), ())

    def test_reset_removing_local_tags_rebuilds_every_affected_output(self):
        app = self.app()
        self.group_all_files(app, metadata={"album": "Локальный альбом"})
        self.complete_outputs(app, self.output_records(app))

        app.reset_source_m4b_groups()

        fresh = self.output_records(app)
        self.assertEqual(app._source_regular_rebuild_paths(fresh), tuple(
            str(record["path"]) for record in fresh if record["kind"] != "m4b"
        ))
        self.assertEqual(app._source_m4b_rebuild_paths(fresh), tuple(
            str(record["path"]) for record in fresh if record["kind"] == "m4b"
        ))

    def test_reset_reordered_chapters_rebuilds_books_but_keeps_individual_outputs(self):
        app = self.app()
        self.group_all_files(app, reverse=True)
        self.complete_outputs(app, self.output_records(app))

        app.reset_source_m4b_groups()

        fresh = self.output_records(app)
        self.assertEqual(app._source_regular_rebuild_paths(fresh), tuple(
            str(record["path"]) for record in fresh if record["kind"] == "group"
        ))
        self.assertEqual(app._source_m4b_rebuild_paths(fresh), tuple(
            str(record["path"]) for record in fresh if record["kind"] == "m4b"
        ))

    def test_reset_changed_group_name_rebuilds_new_book_paths(self):
        app = self.app()
        self.group_all_files(app, name="Особое имя")
        self.complete_outputs(app, self.output_records(app))

        app.reset_source_m4b_groups()

        fresh = self.output_records(app)
        self.assertEqual(app._source_regular_rebuild_paths(fresh), tuple(
            str(record["path"]) for record in fresh if record["kind"] == "group"
        ))
        self.assertEqual(app._source_m4b_rebuild_paths(fresh), tuple(
            str(record["path"]) for record in fresh if record["kind"] == "m4b"
        ))

    def test_reset_does_not_preserve_completion_after_text_or_speech_changes(self):
        for change in ("text", "pause"):
            with self.subTest(change=change):
                app = self.app()
                self.complete_outputs(app, self.output_records(app))
                if change == "text":
                    (self.source / "01.txt").write_text("Совсем новая глава", encoding="utf-8")
                else:
                    app.config["pause_sentence"] += 1

                app.reset_source_m4b_groups()

                fresh = self.output_records(app)
                self.assertEqual(app._source_completed_regular_targets, {})
                self.assertEqual(app._source_completed_m4b_targets, {})
                self.assertEqual(app._source_regular_rebuild_paths(fresh), tuple(
                    str(record["path"]) for record in fresh if record["kind"] != "m4b"
                ))
                self.assertEqual(app._source_m4b_rebuild_paths(fresh), tuple(
                    str(record["path"]) for record in fresh if record["kind"] == "m4b"
                ))

    def test_corrupt_primary_uses_backup_and_other_folder_does_not_get_old_groups(self):
        app = self.app()
        self.add_group(app)
        app._save_source_session()
        app._save_source_session()
        self.session_path.write_text("broken json", encoding="utf-8")
        with self.assertLogs(level="WARNING"):
            restored = self.app()
        self.assertIn("m4b_plan:my-group", restored._source_plan_groups)
        other = self.root / "other"
        other.mkdir()
        (other / "01.txt").write_text("Другая книга", encoding="utf-8")
        restored.config["input_dir"] = str(other)
        restored.load_files()
        self.assertEqual(restored._source_plan_groups, {})
        self.assertEqual(restored._source_path_by_id, {"01.txt": other / "01.txt"})

    def test_empty_group_and_fully_excluded_queue_survive_then_reset(self):
        app = self.app()
        app.tree.delete(*app.tree.get_children())
        app._source_path_by_id = {}
        app.tree.insert("", "end", iid="m4b_plan:empty", text="Будущий том")
        app._source_plan_groups = {
            "m4b_plan:empty": {"name": "Будущий том", "file_ids": (), "metadata_overrides": {"album": "Серия"}}
        }
        app._source_group_ids = {"m4b_plan:empty"}
        app._save_source_session()
        second = self.app()
        self.assertEqual(second._source_tree_file_ids(), [])
        self.assertEqual(second._source_plan_groups["m4b_plan:empty"]["name"], "Будущий том")
        second.reset_source_m4b_groups()
        self.assertEqual(len(second._source_tree_file_ids()), 3)

    def test_recursive_refresh_adds_file_to_existing_folder_without_duplicate_nodes(self):
        nested = self.source / "Том"
        nested.mkdir()
        (nested / "01.txt").write_text("Первая вложенная глава", encoding="utf-8")
        app = self.app()
        app.include_subdirs_var.get.return_value = True
        app.load_files()
        app._save_source_session()
        (nested / "02.txt").write_text("Вторая вложенная глава", encoding="utf-8")
        app.load_files()
        self.assertEqual(app.tree.get_children("dir:Том"), ("Том/01.txt", "Том/02.txt"))
        self.assertEqual(app.tree.get_children("").count("dir:Том"), 1)
        self.assertNotIn("dir:Том", app._source_plan_groups)

    def test_changed_settings_on_restart_discard_completion_but_keep_group(self):
        app = self.app()
        self.add_group(app)
        record = self.record()
        app._remember_source_completed_regular_targets(app._source_plan_revision, [record])
        app._source_plan_dirty = False
        app._save_source_session()
        changed = self.app({"pause_sentence": app.config["pause_sentence"] + 1})
        self.assertTrue(changed._source_plan_dirty)
        self.assertEqual(changed._source_completed_regular_targets, {})
        self.assertIn("m4b_plan:my-group", changed._source_plan_groups)

    def test_runtime_reflow_records_remain_reusable_after_restart(self):
        app = self.app()
        record = self.record(kind="m4b")
        accepted = dict(record, path=self.root / "Новый измеренный том.m4b")
        app._remember_source_runtime_m4b_records([accepted])
        app._source_plan_dirty = False
        app._save_source_session()
        restored = self.app()
        selected = ("01.txt", "02.txt")
        records = restored._reuse_source_runtime_m4b_records([record], selected, restored._source_path_by_id)
        self.assertEqual(records, (accepted,))

    def test_disabling_subfolders_does_not_silently_discard_manual_members(self):
        app = self.app()
        self.add_group(app)
        app._source_plan_groups["m4b_plan:my-group"]["file_ids"] = ("Том/01.txt",)
        app.include_subdirs_var.get.return_value = False
        app.load_files = mock.Mock()
        app._toggle_source_tree_mode()
        app.include_subdirs_var.set.assert_called_once_with(True)
        app._show_warning.assert_called_once()
        app.load_files.assert_not_called()

    def test_temporary_cover_is_shared_by_restored_tree_completed_and_m4b_records(self):
        temp_covers = self.root / "import-covers"
        temp_covers.mkdir()
        cover = temp_covers / "cover.jpg"
        cover.write_bytes(b"test cover bytes")
        durable_covers = self.root / "saved-covers"
        with (mock.patch.object(studio, "SESSION_TEMP_DIR", temp_covers),
              mock.patch.object(studio, "SOURCE_SESSION_COVERS_DIR", durable_covers)):
            app = self.app()
            self.add_group(app)
            group = app._source_plan_groups["m4b_plan:my-group"]
            group["metadata_overrides"]["cover"] = str(cover)
            group["source_group"] = {"metadata_overrides": copy.deepcopy(group["metadata_overrides"])}
            targets = [
                studio.normalize_output_target({"format": "mp3"}),
                studio.normalize_output_target({"format": "m4b"}),
            ]

            def plan(current):
                return studio.plan_source_synthesis_target_paths(
                    self.source, self.root / "outputs", current._source_tree_file_ids(), targets,
                    path_by_id=current._source_path_by_id, source_m4b_groups=current._source_plan_groups,
                )

            records = plan(app)
            app._source_plan_dirty = True
            app._remember_source_completed_regular_targets(app._source_plan_revision, records)
            app._remember_source_runtime_m4b_records(records)
            self.assertTrue(app._save_source_session())
            cover.unlink()
            restored = self.app()
            self.assertEqual(restored._source_regular_rebuild_paths(plan(restored)), ())
            saved_group = restored._source_plan_groups["m4b_plan:my-group"]
            persisted = Path(saved_group["metadata_overrides"]["cover"])
            self.assertEqual(persisted.parent, durable_covers)
            self.assertEqual(persisted.read_bytes(), b"test cover bytes")
            self.assertEqual(saved_group["source_group"]["metadata_overrides"]["cover"], str(persisted))
            covered = [record for record in restored._source_runtime_m4b_records if record.get("metadata_overrides", {}).get("cover")]
            self.assertTrue(covered)
            for record in covered:
                self.assertEqual(record["metadata_overrides"]["cover"], str(persisted))

    def test_adding_and_reordering_targets_keeps_completed_audio_current(self):
        app = self.app()
        record = self.record()
        app.config["synthesis_targets"] = [record["target"]]
        app._check_source_session_changes()
        app._remember_source_completed_regular_targets(app._source_plan_revision, [record])
        app._source_plan_dirty = False
        app.config["synthesis_targets"].insert(0, studio.normalize_output_target({"format": "mp3", "assembly_mode": "merge", "output_dir": "groups"}))
        app.config["source_output_targets"] = copy.deepcopy(app.config["synthesis_targets"])
        app._check_source_session_changes()
        self.assertFalse(app._source_plan_dirty)
        moved_record = dict(record, target_index=1)
        new_record = dict(record, target_index=0, kind="group", path=self.root / "group.mp3")
        self.assertEqual(app._source_regular_rebuild_paths([new_record, moved_record]), ())
        app._save_source_session()
        restored = self.app({"synthesis_targets": app.config["synthesis_targets"], "source_output_targets": app.config["source_output_targets"]})
        self.assertFalse(restored._source_plan_dirty)
        self.assertEqual(restored._source_regular_rebuild_paths([new_record, moved_record]), ())

    def test_only_changed_target_bitrate_requires_rebuild(self):
        app = self.app()
        first = self.record()
        second = dict(first, target_index=1, path=self.root / "other.mp3")
        app._remember_source_completed_regular_targets(app._source_plan_revision, [first, second])
        app._source_plan_dirty = False
        changed = copy.deepcopy(first)
        changed["target"]["bitrate"] = "192k"
        self.assertEqual(app._source_regular_rebuild_paths([changed, second]), (str(first["path"]),))

    def test_old_signature_migrates_without_rebuilding_unchanged_outputs(self):
        app = self.app()
        app._source_session_config_signature = app._source_session_settings_signature(legacy_targets=True)
        app._source_plan_dirty = False
        app._save_source_session()
        restored = self.app()
        self.assertFalse(restored._source_plan_dirty)

    def test_runtime_m4b_survives_inserting_ordinary_target_before_it(self):
        app = self.app()
        record = self.record(kind="m4b")
        runtime = dict(record, path=self.root / "Измеренный том.m4b")
        app._remember_source_runtime_m4b_records([runtime])
        shifted = dict(record, target_index=1)
        ordinary = self.record()
        reused = app._reuse_source_runtime_m4b_records([ordinary, shifted], ("01.txt", "02.txt"), app._source_path_by_id)
        self.assertEqual(reused, (ordinary, dict(runtime, target_index=1)))
        self.assertEqual(app._source_m4b_rebuild_paths(reused), ())

    def test_changed_m4b_target_rebuilds_its_path_and_keeps_other_target(self):
        app = self.app()
        first = self.record(kind="m4b")
        second = copy.deepcopy(first)
        second.update(target_index=1, path=self.root / "other.m4b")
        second["target"]["bitrate"] = "128k"
        app._remember_source_runtime_m4b_records([first, second])
        changed = copy.deepcopy(first)
        changed["target"]["bitrate"] = "96k"
        reused = app._reuse_source_runtime_m4b_records([changed, second], ("01.txt", "02.txt"), app._source_path_by_id)
        self.assertEqual(reused, (changed, second))
        self.assertEqual(app._source_m4b_rebuild_paths(reused), (str(first["path"]),))

    def test_completed_selected_m4b_is_current_while_other_parts_remain_dirty(self):
        app = self.app()
        app._source_plan_dirty = True
        first = self.record(kind="m4b")
        unfinished = dict(first, path=self.root / "unfinished.m4b")
        app._remember_source_runtime_m4b_records([first])

        reused = app._reuse_source_runtime_m4b_records(
            [first], first["file_ids"], app._source_path_by_id,
        )

        self.assertEqual(reused, (first,))
        self.assertEqual(app._source_m4b_rebuild_paths([first, unfinished]), (str(unfinished["path"]),))
        self.assertTrue(app._source_plan_dirty)
        app._save_source_session()
        restored = self.app()
        self.assertTrue(restored._source_plan_dirty)
        self.assertEqual(restored._source_m4b_rebuild_paths([first, unfinished]), (str(unfinished["path"]),))

    def test_selected_m4b_boundaries_survive_another_selected_run_and_restart(self):
        app = self.app()
        app._source_plan_dirty = True
        first = self.record(kind="m4b")
        first["path"] = self.root / "Принятая часть A.m4b"
        second = dict(
            first, path=self.root / "Принятая часть B.m4b", target_index=1,
            file_ids=("03.txt",), source_paths=(self.source / "03.txt",),
        )
        # Индекс M4B поменялся после добавления обычной цели между запусками.
        app._remember_source_runtime_m4b_records([first])
        app._remember_source_runtime_m4b_records([second])
        planned = dict(first, path=self.root / "Предварительная часть.m4b", target_index=1)
        expected = dict(first, target_index=1)

        self.assertEqual(app._reuse_source_runtime_m4b_records(
            [planned], first["file_ids"], app._source_path_by_id,
        ), (expected,))
        self.assertEqual(app._source_m4b_rebuild_paths([expected, second]), ())
        app._save_source_session()
        restored = self.app()
        self.assertEqual(restored._reuse_source_runtime_m4b_records(
            [planned], first["file_ids"], restored._source_path_by_id,
        ), (expected,))
        self.assertEqual(len(restored._source_runtime_m4b_records), 2)

    def test_selecting_only_one_chapter_of_accepted_volume_uses_fresh_plan(self):
        app = self.app()
        accepted = self.record(kind="m4b")
        app._remember_source_runtime_m4b_records([accepted])
        partial = dict(
            accepted, path=self.root / "Только первая глава.m4b",
            file_ids=("01.txt",), source_paths=(self.source / "01.txt",),
        )

        self.assertEqual(app._reuse_source_runtime_m4b_records(
            [partial], partial["file_ids"], app._source_path_by_id,
        ), (partial,))

    def test_accepting_new_split_discards_overlapping_old_volume(self):
        app = self.app()
        old_volume = self.record(kind="m4b")
        unrelated = dict(
            old_volume, path=self.root / "Третья глава.m4b",
            file_ids=("03.txt",), source_paths=(self.source / "03.txt",),
        )
        app._remember_source_runtime_m4b_records([old_volume, unrelated])
        split = tuple(dict(
            old_volume, path=self.root / f"Новая часть {number}.m4b",
            file_ids=(file_id,), source_paths=(self.source / file_id,),
        ) for number, file_id in enumerate(old_volume["file_ids"], 1))

        app._remember_source_runtime_m4b_records(split)

        self.assertEqual(app._source_runtime_m4b_records, (*split, unrelated))
        self.assertNotIn(old_volume, app._source_runtime_m4b_records)

    def test_real_text_change_invalidates_accepted_partial_m4b(self):
        app = self.app()
        app._source_plan_dirty = True
        accepted = self.record(kind="m4b")
        app._remember_source_runtime_m4b_records([accepted])
        (self.source / "01.txt").write_text("Совсем другой текст главы.", encoding="utf-8")

        app._check_source_session_changes()

        self.assertIsNone(app._source_runtime_m4b_records)
        self.assertEqual(app._source_m4b_rebuild_paths([accepted]), (str(accepted["path"]),))

    def test_group_metadata_change_invalidates_accepted_partial_m4b(self):
        app = self.app()
        self.add_group(app)
        accepted = self.record(kind="m4b")
        app._remember_source_runtime_m4b_records([accepted])

        self.assertTrue(app._commit_source_group_metadata_overrides(
            "m4b_plan:my-group", {"album": "Новый альбом"},
        ))

        self.assertIsNone(app._source_runtime_m4b_records)
        self.assertEqual(app._source_m4b_rebuild_paths([accepted]), (str(accepted["path"]),))

    def test_speech_settings_change_invalidates_accepted_partial_m4b(self):
        app = self.app()
        accepted = self.record(kind="m4b")
        app._remember_source_runtime_m4b_records([accepted])
        app.config["pause_sentence"] += 1

        app._check_source_session_changes()

        self.assertIsNone(app._source_runtime_m4b_records)
        self.assertEqual(app._source_m4b_rebuild_paths([accepted]), (str(accepted["path"]),))


if __name__ == "__main__":
    unittest.main()

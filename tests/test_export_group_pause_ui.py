"""Точные поля пауз группы и их доступность для фактических целей экспорта."""

import copy
import types
import unittest
from unittest import mock

from test_regressions import studio


class ExportGroupPauseUiTests(unittest.TestCase):
    def setUp(self):
        self.interpreter = studio.tk.Tcl()
        self.app = object.__new__(studio.TTSApp)
        self.app.grp_pause_start_var = studio.tk.IntVar(self.interpreter, 250)
        self.app.grp_pause_var = studio.tk.IntVar(self.interpreter, 750)
        self.app.grp_pause_end_var = studio.tk.IntVar(self.interpreter, 500)

    def test_fractional_tcl_value_is_rejected_without_intvar_rounding(self):
        for field in (self.app.grp_pause_start_var, self.app.grp_pause_var, self.app.grp_pause_end_var):
            for raw in ("", "-1", "2.9", "1e3", "nan", "abc"):
                with self.subTest(field=str(field), raw=raw):
                    previous = field._tk.globalgetvar(field._name)
                    field._tk.globalsetvar(field._name, raw)
                    with self.assertRaisesRegex(ValueError, "целыми числами"):
                        self.app._read_export_group_pauses()
                    field._tk.globalsetvar(field._name, previous)

    def test_unchecked_mass_pause_fields_are_not_validated(self):
        self.app.grp_pause_start_var.set("")
        self.app.grp_pause_end_var.set("-2.9")
        self.assertEqual(self.app._read_export_group_pauses({"pause"}), {"pause": 750})
        self.assertEqual(self.app._read_export_group_pauses(set()), {})
        self.app.grp_pause_var.set("00012")
        self.assertEqual(self.app._read_export_group_pauses({"pause"}), {"pause": 12})

    def test_incomplete_editor_never_commits_partial_pause_values(self):
        app = self.app
        app.current_selected_export_item = "group"
        app.export_groups = {"group": dict(name="Группа", pause_start=250, pause=750, pause_end=500)}
        app.export_files = {}
        app.export_tree = types.SimpleNamespace(exists=lambda _item: True, item=mock.Mock())
        app.grp_merge_var = studio.tk.BooleanVar(self.interpreter, True)
        app.grp_subfolder_var = studio.tk.BooleanVar(self.interpreter, False)
        for name in ("name", "artist", "album", "album_artist", "genre", "composer", "year", "cover"):
            setattr(app, "grp_" + name + "_var", studio.tk.StringVar(self.interpreter, "Группа" if name == "name" else ""))
        app._schedule_export_project_save = mock.Mock()
        app.grp_pause_start_var.set(100)
        app.grp_pause_var.set("")
        app.grp_pause_end_var.set(900)
        app.save_export_item_settings()
        self.assertEqual({key: app.export_groups["group"][key] for key in ("pause_start", "pause", "pause_end")},
                         {"pause_start": 250, "pause": 750, "pause_end": 500})
        app.grp_pause_var.set(0)
        app.save_export_item_settings()
        self.assertEqual({key: app.export_groups["group"][key] for key in ("pause_start", "pause", "pause_end")},
                         {"pause_start": 100, "pause": 0, "pause_end": 900})

    def test_pause_controls_follow_effective_assembly_and_selection(self):
        app = self.app
        app.current_selected_export_item = "group"
        app.export_groups = {"group": {}}
        app.export_tags_only_var = studio.tk.BooleanVar(self.interpreter, False)
        app.grp_merge_var = studio.tk.BooleanVar(self.interpreter, False)
        app.grp_subfolder_var = studio.tk.BooleanVar(self.interpreter, True)
        app.export_fmt_var = studio.tk.StringVar(self.interpreter, "mp3")
        app.chk_subfolder = mock.Mock()
        for name in ("ent_pause_start", "ent_pause", "ent_pause_end"):
            setattr(app, name, mock.Mock())

        def require(enabled):
            app._sync_group_subfolder_state()
            expected = studio.tk.NORMAL if enabled else studio.tk.DISABLED
            for name in ("ent_pause_start", "ent_pause", "ent_pause_end"):
                self.assertEqual(getattr(app, name).configure.call_args.kwargs["state"], expected)

        app.export_targets = []
        require(False)
        app.export_fmt_var.set("m4b")
        require(True)
        app.export_targets = [dict(format="mp3", assembly_mode="files")]
        app.grp_merge_var.set(True)
        require(False)
        app.grp_merge_var.set(False)
        app.export_targets = [dict(format="mp3", assembly_mode="merge")]
        require(True)
        app.export_targets = [dict(format="mp3", assembly_mode="files"), dict(format="m4b", enabled=False)]
        require(False)
        app.export_targets[1]["enabled"] = True
        require(True)
        app.export_tags_only_var.set(True)
        require(False)
        app.export_tags_only_var.set(False)
        app._export_running = True
        require(False)
        app._export_running = False
        app._export_lock = True
        require(False)
        app._export_lock = False
        require(True)
        app.current_selected_export_item = "file"
        require(False)
        app.current_selected_export_item = None
        require(False)

    def test_project_round_trip_preserves_all_three_group_pauses(self):
        groups = {"group": dict(name="Группа", merge=True, pause_start=321, pause=654, pause_end=987)}
        original = copy.deepcopy(groups)
        session = studio.build_export_project_session(("group",), {"group": ()}, groups, {})
        restored = studio.normalize_export_project_session(session)
        self.assertEqual(groups, original)
        self.assertEqual({key: restored["items"][0]["settings"][key] for key in ("pause_start", "pause", "pause_end")},
                         {"pause_start": 321, "pause": 654, "pause_end": 987})

    def test_subfolder_and_hint_follow_m4b_and_target_assembly(self):
        app = self.app
        app.current_selected_export_item = "group"
        app.export_groups = {"group": {}}
        app.export_tags_only_var = studio.tk.BooleanVar(self.interpreter, False)
        app.grp_merge_var = studio.tk.BooleanVar(self.interpreter, False)
        app.grp_subfolder_var = studio.tk.BooleanVar(self.interpreter, True)
        app.export_fmt_var = studio.tk.StringVar(self.interpreter, "m4b")
        app.chk_subfolder = mock.Mock()
        app.lbl_subfolder_hint = mock.Mock()
        app.export_targets = []
        app._sync_group_subfolder_state()
        self.assertEqual(app.chk_subfolder.configure.call_args.kwargs["state"], studio.tk.DISABLED)
        self.assertIn("M4B", app.lbl_subfolder_hint.configure.call_args.kwargs["text"])
        self.assertIn("подпапка не используется", app.lbl_subfolder_hint.configure.call_args.kwargs["text"])
        app.export_targets = [dict(format="m4b", assembly_mode="merge")]
        app._sync_group_subfolder_state()
        self.assertEqual(app.chk_subfolder.configure.call_args.kwargs["state"], studio.tk.DISABLED)
        app.grp_merge_var.set(True)
        app.export_targets = [dict(format="mp3", assembly_mode="files")]
        app._sync_group_subfolder_state()
        self.assertEqual(app.chk_subfolder.configure.call_args.kwargs["state"], studio.tk.NORMAL)
        app.export_targets.append(dict(format="m4b", assembly_mode="merge"))
        app._sync_group_subfolder_state()
        self.assertEqual(app.chk_subfolder.configure.call_args.kwargs["state"], studio.tk.NORMAL)

    def test_tree_split_preserves_assembly_for_other_inherited_formats(self):
        for merge in (False, True):
            with self.subTest(merge=merge):
                app = self.app
                original = dict(name="Серия. Том 5", merge=merge, pause_start=200,
                                pause=300, pause_end=400, album="Серия", cover="обложка.jpg")
                app.export_groups = {"group": original}
                file_ids = ("chapter_1", "chapter_2")
                app.export_files = {file_id: dict(path=file_id + ".wav", duration=1.0)
                                    for file_id in file_ids}
                app.export_tree = mock.Mock()
                app.export_tree.get_children.side_effect = lambda item: ("group",) if item == "" else file_ids
                app.export_tree.exists.return_value = True
                app.export_tree.selection.return_value = ()
                app._export_file_duration_seconds = lambda file_id: app.export_files[file_id]["duration"]
                for method in ("update_group_duration", "_clear_export_item_editor", "_disable_export_settings",
                               "update_total_export_duration", "_refresh_export_tree_action_states",
                               "_schedule_export_project_save"):
                    setattr(app, method, mock.Mock())
                result = app._split_export_groups_by_m4b_limits({"format": "m4b", "max_chapters": 1})
                self.assertEqual(result["parts"], 2)
                self.assertEqual(len(app.export_groups), 2)
                for settings in app.export_groups.values():
                    for key in ("merge", "pause_start", "pause", "pause_end", "album", "cover"):
                        self.assertEqual(settings[key], original[key])
                    self.assertEqual(studio.output_target_merges_group(
                        {"format": "mp3", "assembly_mode": "inherit"}, settings), merge)
                    self.assertTrue(studio.output_target_merges_group({"format": "m4b"}, settings))
if __name__ == "__main__":
    unittest.main()

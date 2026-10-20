"""Export summaries reflect legacy M4B settings without requiring a profile."""
import copy
import importlib.util
from pathlib import Path
import sys
import tkinter as tk
import unittest
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


class LegacyExportSummaryTests(unittest.TestCase):
    def setUp(self):
        self.interpreter = tk.Tcl()
        self.app = object.__new__(studio.TTSApp)
        self.app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        self.app.export_targets = []
        self.app.lbl_export_audio_profile_summary = object()
        self.app._set_export_profile_summary = mock.Mock()
        for attr, value in {
            "export_fmt_var": "m4b",
            "export_bitrate_var": "192k",
            "export_sample_rate_var": "44100",
            "export_channels_var": "mono",
            "export_outdir_var": "/tmp/export",
            "export_m4b_bitrate_var": "96k",
            "export_m4b_template_var": "{book}. Том {volume}",
            "export_m4b_hours_var": "12",
            "export_m4b_chapters_var": "25",
        }.items():
            setattr(self.app, attr, tk.StringVar(self.interpreter, value=value))
        self.app.export_tags_only_var = tk.BooleanVar(self.interpreter, value=False)
        self.app.export_apply_fx_var = tk.BooleanVar(self.interpreter, value=True)
        self.app.exp_speed_var = tk.DoubleVar(self.interpreter, value=1.2)
        self.app.exp_pitch_var = tk.DoubleVar(self.interpreter, value=1.1)
        self.app.exp_echo_var = tk.BooleanVar(self.interpreter, value=True)
        self.app.exp_delay_var = tk.IntVar(self.interpreter, value=400)
        self.app.exp_decay_var = tk.DoubleVar(self.interpreter, value=0.4)

    def summary(self):
        self.app._refresh_output_targets_summary()
        return self.app._set_export_profile_summary.call_args.args[0]

    def test_legacy_m4b_shows_actual_aac_parameters_and_limits(self):
        text = self.summary()
        for expected in (
            "M4B AAC 96k", "44100 Гц", "mono", "эффекты не применяются",
            "{book}. Том {volume}", "12 ч / 25 глав", "/tmp/export",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("192k", text)
        self.assertNotIn("недоступны", text)
        self.app.export_m4b_bitrate_var.set("64k")
        self.app.export_m4b_hours_var.set("8")
        self.app.export_m4b_chapters_var.set("40")
        updated = self.summary()
        self.assertIn("M4B AAC 64k", updated)
        self.assertIn("8 ч / 40 глав", updated)

    def test_ordinary_format_keeps_current_profile_summary(self):
        self.app.export_fmt_var.set("mp3")
        expected = self.app._current_audio_profile_summary("export")
        self.assertEqual(self.summary(), expected)
        self.assertIn("MP3", expected)

    def test_disabled_target_set_does_not_show_legacy_m4b(self):
        self.app.export_targets = [{"format": "m4b", "enabled": False}]
        self.assertIn("все его цели отключены", self.summary())

    def test_tags_only_takes_precedence_over_legacy_m4b(self):
        self.app.export_tags_only_var.set(True)
        text = self.summary()
        self.assertIn("Обновление тегов", text)
        self.assertIn("конвертация, склейка и эффекты не применяются", text)
        self.assertNotIn("AAC", text)


if __name__ == "__main__":
    unittest.main()

"""Проверки ограниченного предпросмотра и отказов запуска задач без Tk-окна."""

import copy
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_regressions import studio
import test_audit_export


class Widget:
    def __init__(self, parent=None, **options):
        self.options = options
        self.children = []
        self.rows = {}
        self.callbacks = {}
        self.alive = True
        if parent is not None:
            parent.children.append(self)

    def configure(self, **options):
        self.options.update(options)

    config = configure

    def insert(self, parent, position, *, iid=None, values=None):
        if iid is not None:
            self.rows[iid] = values

    def delete(self, *items):
        for item in items:
            self.rows.pop(item, None)

    def get_children(self, _parent=""):
        return tuple(self.rows)

    def after(self, _delay, callback):
        identifier = len(self.callbacks) + 1
        self.callbacks[identifier] = callback
        return identifier

    def after_cancel(self, identifier):
        self.callbacks.pop(identifier, None)

    def winfo_exists(self):
        return self.alive

    def grab_current(self):
        return None

    def destroy(self):
        self.alive = False

    def __getattr__(self, name):
        if name in {"pack", "grid", "columnconfigure", "rowconfigure", "withdraw",
                    "title", "transient", "grab_set", "grab_release", "minsize", "heading",
                    "column", "yview", "xview", "set", "bind", "focus_set"}:
            return lambda *args, **kwargs: None
        if name == "protocol":
            return lambda key, callback: self.options.__setitem__(key, callback)
        raise AttributeError(name)


class Variable:
    def __init__(self, **_kwargs):
        self.value = ""

    def get(self):
        return self.value

    def trace_add(self, *_args):
        pass


class ControlledThread:
    def __init__(self, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        pass

    def run(self):
        self.target(*self.args)


class FullPreviewBackpressureTests(unittest.TestCase):
    def setUp(self):
        self.app = object.__new__(studio.TTSApp)
        self.app.root = Widget()
        self.app.get_status_color = lambda _status: "black"
        self.app._center_popup = mock.Mock()
        self.pending = queue.Queue()
        self.app._post_to_ui = lambda callback, *args: self.pending.put((callback, args))
        self.threads = []
        self.current = {"number": 1, "nested": {"value": "original"}}
        self.observations = []
        self.provider_error = None

        def provider():
            if self.provider_error is not None:
                raise self.provider_error
            return self.current

        self.provider = provider
        self.factory = mock.Mock(side_effect=self.thread)
        self.render = mock.Mock(side_effect=self.render_rows)
        for owner, name, value in [
            (studio.tk, "Toplevel", Widget), (studio.tk, "Text", Widget),
            (studio.tk, "StringVar", Variable), (studio.sys, "platform", "linux"),
            (studio.threading, "Thread", self.factory),
            (studio, "template_helper_full_preview_rows", self.render),
        ] + [(studio.ttk, name, Widget) for name in
             ("Frame", "Label", "Entry", "Treeview", "Scrollbar", "Button")]:
            patch = mock.patch.object(owner, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def thread(self, *args, **kwargs):
        thread = ControlledThread(*args, **kwargs)
        self.threads.append(thread)
        return thread

    def render_rows(self, _kind, _template, data):
        self.observations.append(copy.deepcopy(data))
        if data.get("error"):
            raise ValueError("stale worker error")
        return ({"source": "input", "result": str(data["number"]),
                 "group": "group", "path": "", "detail": ""},)

    def walk(self, widget):
        yield widget
        for child in widget.children:
            yield from self.walk(child)

    def open(self):
        self.window = self.app._open_template_full_preview(
            "output_file", "{name}", {}, preview_data_provider=self.provider,
        )
        widgets = list(self.walk(self.window))
        self.refresh = next(w.options["command"] for w in widgets
                            if w.options.get("text") == "Обновить")
        self.table = next(w for w in widgets if "columns" in w.options)

    def drain(self):
        while not self.pending.empty():
            callback, args = self.pending.get_nowait()
            callback(*args)

    def test_repeated_refresh_runs_only_first_and_latest_deep_snapshot(self):
        self.open()
        for number in range(2, 31):
            self.current["number"] = number
            self.refresh()
        self.assertEqual(len(self.threads), 1)
        self.current["nested"]["value"] = "changed after request"
        self.threads[0].run()
        self.drain()
        self.assertEqual(len(self.threads), 2)
        self.assertEqual(self.table.rows, {})
        self.threads[1].run()
        self.drain()
        self.assertEqual([item["number"] for item in self.observations], [1, 30])
        self.assertEqual(self.observations[-1]["nested"]["value"], "original")
        self.assertEqual(self.table.rows["0"][1], "30")

    def test_stale_failure_releases_worker_and_starts_latest_request(self):
        self.current["error"] = True
        self.open()
        self.current.update(number=2, error=False)
        self.refresh()
        self.threads[0].run()
        self.drain()
        self.assertEqual(len(self.threads), 2)
        self.threads[1].run()
        self.drain()
        self.assertEqual(self.table.rows["0"][1], "2")
        self.assertFalse(any("stale worker error" in str(w.options.get("text", ""))
                             for w in self.walk(self.window)))

    def test_invalid_new_context_discards_previous_pending_request(self):
        self.open()
        self.current["number"] = 2
        self.refresh()
        self.provider_error = ValueError("current context invalid")
        self.refresh()
        self.threads[0].run()
        self.drain()
        self.assertEqual(len(self.threads), 1)
        self.assertEqual(self.table.rows, {})
        self.assertTrue(any("current context invalid" in str(w.options.get("text", ""))
                            for w in self.walk(self.window)))

    def test_close_discards_pending_request_and_late_completion(self):
        self.open()
        self.current["number"] = 2
        self.refresh()
        self.window.options["WM_DELETE_WINDOW"]()
        self.threads[0].run()
        self.drain()
        self.assertEqual(len(self.threads), 1)
        self.assertFalse(self.window.alive)
        self.assertEqual(self.table.rows, {})

    def test_creation_and_start_failure_allow_next_refresh(self):
        for stage in ("create", "start"):
            with self.subTest(stage=stage):
                self.factory.side_effect = RuntimeError("creation failed") if stage == "create" else self.thread
                with mock.patch.object(ControlledThread, "start", side_effect=RuntimeError("start failed")):
                    self.open()
                self.factory.side_effect = self.thread
                before = len(self.threads)
                self.refresh()
                self.assertEqual(len(self.threads), before + 1)
                self.threads[-1].run()
                self.drain()
                self.assertEqual(self.table.rows["0"][1], "1")


class ThreadCreationRecoveryTests(unittest.TestCase):
    def app(self):
        app = object.__new__(studio.TTSApp)
        app.root = mock.Mock()
        app.config = copy.deepcopy(studio.DEFAULT_CONFIG)
        for name in ("_set_status_label", "_show_task_notification", "_show_error"):
            setattr(app, name, mock.Mock())
        return app

    def test_import_constructor_failure_releases_busy_before_notification(self):
        app = self.app()
        app.import_filepaths = ["book.txt"]
        app.import_filepath_var = mock.Mock(get=lambda: "")
        app.settings_vars = {name: mock.Mock(get=lambda value=value: value) for name, value in
                             {"import_template": "{name}", "import_regex": "", "import_single_file": False}.items()}
        app._update_import_queue_actions = mock.Mock()
        app.lbl_import_status = mock.Mock()
        app.save_settings = mock.Mock()
        app._show_task_notification.side_effect = lambda *_args: self.assertFalse(app._import_running)
        with mock.patch.object(studio.threading, "Thread", side_effect=RuntimeError("no thread")):
            app.start_import()
        self.assertIsNone(app._import_thread)
        self.assertEqual(app._update_import_queue_actions.call_count, 2)
        app._show_task_notification.assert_called_once()

    def test_normalization_constructor_failure_restores_buttons(self):
        app = self.app()
        app._normalizer_batch_running = False
        app._normalizer_batch_cancel = threading.Event()
        for name in ("btn_batch_normalize_text", "btn_cancel_batch_normalize", "lbl_normalizer_preview_status"):
            setattr(app, name, mock.Mock())
        with mock.patch.object(studio.threading, "Thread", side_effect=RuntimeError("no thread")):
            app._start_batch_normalization(Path("source"), Path("output"), object(),
                                           include_subdirs=False, overwrite=False, files=["one.txt"])
        self.assertFalse(app._normalizer_batch_running)
        self.assertIsNone(app._normalizer_batch_thread)
        self.assertEqual(app.btn_batch_normalize_text.configure.call_args.kwargs["state"], studio.tk.NORMAL)
        self.assertEqual(app.btn_cancel_batch_normalize.configure.call_args.kwargs["state"], studio.tk.DISABLED)
        app._show_task_notification.assert_called_once()

    def test_sqlite_close_constructor_and_start_failure_preserve_application(self):
        for stage in ("create", "start"):
            with self.subTest(stage=stage):
                app = self.app()
                app.batch_processor = SimpleNamespace(cache_backend="sqlite", _sqlite_cache_reference=True)
                app.direct_processor = None
                app._create_wait_popup = mock.Mock(return_value=mock.Mock())
                app._close_popup_safely = mock.Mock()
                patch = (mock.patch.object(studio.threading, "Thread", side_effect=RuntimeError("no thread"))
                         if stage == "create" else
                         mock.patch.object(studio.threading.Thread, "start", side_effect=RuntimeError("no thread")))
                with patch:
                    app._complete_application_close()
                self.assertFalse(app._is_closing)
                self.assertIsNone(app._sqlite_close_worker)
                self.assertIsNone(app._sqlite_close_popup)
                app.root.destroy.assert_not_called()
                app._show_error.assert_called_once()
                app._close_popup_safely.assert_called_once()

    def test_tag_export_constructor_failure_releases_export_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "chapter.mp3"
            source.write_bytes(b"source")
            app = test_audit_export.GroupTagInheritanceTests.make_app(source)
            app._show_task_notification = mock.Mock()
            app._set_export_running_state = lambda value: setattr(app, "_export_running", value)
            with mock.patch.object(studio.threading, "Thread", side_effect=RuntimeError("no thread")):
                app.start_export_process()
            self.assertFalse(app._export_running)
            self.assertIsNone(app._export_thread)
            app._show_task_notification.assert_called_once()

    def test_process_queue_pool_records_only_started_workers_for_fault_cleanup(self):
        """Late pool launch failure leaves no unconsumed sentinel or live worker."""
        real_thread, real_queue = threading.Thread, queue.Queue
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("file", "m4b"):
                for failure_number in (1, 2):
                    with self.subTest(kind=kind, failure_number=failure_number):
                        app = self.app()
                        app._post_to_ui = mock.Mock()
                        app.finish_processing = mock.Mock()
                        app.update_progress_ui = mock.Mock()
                        app.update_file_status = mock.Mock()
                        app._source_path_by_id = {}
                        processor = SimpleNamespace(
                            cfg={"use_cache": False}, is_stopped=False, active_threads=[],
                            flush_cache=mock.Mock(return_value=True),
                            _save_processing_statuses=mock.Mock(return_value=True),
                            close_cache=mock.Mock(return_value=True),
                            cleanup_transient_audio_files=mock.Mock(), processing_statuses_ram={},
                        )
                        config = copy.deepcopy(studio.DEFAULT_CONFIG)
                        config.update(input_dir=directory, output_dir=directory,
                                      source_multi_output=True, use_cache=False,
                                      max_parallel_encodes=2, source_m4b_auto_split_long=False)
                        config["source_target_records"] = [
                            {"kind": kind, "item_id": f"chapter{number}",
                             "file_ids": (f"chapter{number}",),
                             "path": Path(directory) / f"{number}.{kind}", "target_index": 0,
                             "target": {"format": "m4b" if kind == "m4b" else "mp3"}}
                            for number in range(2)
                        ]
                        queues, workers = [], []

                        def capture_queue(*args, **kwargs):
                            captured = real_queue(*args, **kwargs)
                            queues.append(captured)
                            return captured

                        def launch(*args, **kwargs):
                            worker = real_thread(*args, **kwargs)
                            if len(workers) + 1 == failure_number:
                                worker.start = mock.Mock(side_effect=RuntimeError("injected worker launch failure"))
                            workers.append(worker)
                            return worker

                        with (mock.patch.object(studio.threading, "Thread", side_effect=launch),
                              mock.patch.object(studio.queue, "Queue", side_effect=capture_queue),
                              mock.patch.object(studio, "source_output_file_is_ready", return_value=False)):
                            app.process_queue(processor, ["chapter0", "chapter1"], config, False)
                        self.assertEqual(len(workers), failure_number)
                        self.assertFalse(any(worker.is_alive() for worker in workers))
                        self.assertTrue(queues)
                        self.assertTrue(all(captured.unfinished_tasks == 0 and captured.empty() for captured in queues))
                        terminal = [call for call in app._post_to_ui.call_args_list
                                    if call.args[0] is app.finish_processing]
                        self.assertEqual(len(terminal), 1)
                        self.assertTrue(terminal[0].args[2])
                        processor.flush_cache.assert_called_once()
                        processor.close_cache.assert_called_once()


if __name__ == "__main__":
    unittest.main()

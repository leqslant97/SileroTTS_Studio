"""Cache transcode callbacks distinguish complete and partial results without Tk."""
import unittest
from unittest import mock

from tests.test_regressions import studio


class CacheTranscodeNotificationTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(studio.TTSApp)
        app._cache_transcode_cancel = object()
        app._cache_ui_backend = 'sqlite'
        app._cache_ui_loaded = True
        for name in ('_end_cache_operation', '_close_popup_safely',
                     '_cache_generation_changed', 'load_cache_ui', '_show_task_notification'):
            setattr(app, name, mock.Mock())
        return app

    def finish(self, stats, error=None):
        app = self.make_app()
        popup = object()
        app._finish_cache_transcode(popup, stats, error)
        self.assertIsNone(app._cache_transcode_cancel)
        app._end_cache_operation.assert_called_once_with('transcode')
        app._close_popup_safely.assert_called_once_with(popup)
        app._cache_generation_changed.assert_called_once_with()
        app.load_cache_ui.assert_called_once_with()
        app._show_task_notification.assert_called_once()
        return app._show_task_notification.call_args.args

    def test_failed_or_missing_files_are_reported_as_partial_warning(self):
        for counts in ({'failed': 1}, {'missing': 1}, {'failed': 2, 'missing': 3}):
            with self.subTest(counts=counts):
                severity, title, message = self.finish({'converted': 4, **counts})
                self.assertEqual(severity, 'warning')
                self.assertEqual(title, 'Перекодирование завершено')
                self.assertIn('Перекодировано Vorbis → Opus: 4', message)
                self.assertIn(f"Ошибок: {counts.get('failed', 0)}", message)
                self.assertIn(f"Отсутствовали на диске: {counts.get('missing', 0)}", message)

    def test_complete_success_retains_info_and_size_totals(self):
        severity, title, message = self.finish({'converted': 2, 'already_opus': 1,
            'old_size': 4096, 'new_size': 2048, 'old_allocated': 8192, 'new_allocated': 4096})
        self.assertEqual((severity, title), ('info', 'Перекодирование завершено'))
        self.assertIn('Экономия логического размера: 2.00 КиБ', message)
        self.assertIn('Экономия занятого места: 4.00 КиБ', message)

    def test_cancellation_keeps_restart_hint_and_partial_failure_warning(self):
        for failed, expected in ((0, 'info'), (1, 'warning')):
            with self.subTest(failed=failed):
                severity, title, message = self.finish({'cancelled': True, 'failed': failed})
                self.assertEqual((severity, title), (expected, 'Перекодирование остановлено'))
                self.assertIn('Операцию можно запустить снова', message)

    def test_fatal_error_takes_precedence_over_partial_counts(self):
        self.assertEqual(self.finish({'failed': 1}, error='Диск заполнен'),
                         ('error', 'Ошибка', 'Диск заполнен'))


if __name__ == '__main__':
    unittest.main()

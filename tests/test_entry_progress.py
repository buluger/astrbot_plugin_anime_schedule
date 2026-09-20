import unittest

from entry_progress import (
    _is_paused,
    _mark_watched_one,
    _normalize_entry,
    _pause_one,
    _progress_label,
    _resume_one,
    _set_watched_one,
    _undo_watched_one,
)


def _entry(**kwargs) -> dict:
    data = {
        "title": "测试番",
        "watched_ep": 3,
        "max_ep": 12,
        "last_watched_date": None,
    }
    data.update(kwargs)
    return _normalize_entry(data)


class NormalizeEntryTest(unittest.TestCase):
    def test_old_data_defaults_not_paused(self):
        entry = _normalize_entry({"title": "旧番", "watched_ep": 2, "max_ep": 12})
        self.assertFalse(entry["paused"])

    def test_string_true_coerced(self):
        entry = _normalize_entry({"title": "旧番", "paused": "true"})
        self.assertTrue(entry["paused"])


class PauseResumeTest(unittest.TestCase):
    def test_pause_then_resume(self):
        entry = _entry()
        ok, msg = _pause_one(entry)
        self.assertTrue(ok)
        self.assertTrue(_is_paused(entry))
        self.assertIn("已暂停", msg)

        ok, msg = _resume_one(entry)
        self.assertTrue(ok)
        self.assertFalse(_is_paused(entry))
        self.assertIn("已恢复", msg)

    def test_double_pause_fails(self):
        entry = _entry(paused=True)
        ok, msg = _pause_one(entry)
        self.assertFalse(ok)
        self.assertIn("已经是停播", msg)

    def test_resume_when_not_paused_fails(self):
        entry = _entry()
        ok, msg = _resume_one(entry)
        self.assertFalse(ok)
        self.assertIn("未停播", msg)


class WatchedSkipPausedTest(unittest.TestCase):
    def test_mark_skips_paused(self):
        entry = _entry(paused=True, watched_ep=3)
        ok, msg = _mark_watched_one(entry)
        self.assertFalse(ok)
        self.assertEqual(entry["watched_ep"], 3)
        self.assertIn("已停播，已跳过", msg)

    def test_mark_increments_active(self):
        entry = _entry(watched_ep=3)
        ok, msg = _mark_watched_one(entry)
        self.assertTrue(ok)
        self.assertEqual(entry["watched_ep"], 4)
        self.assertIn("已看第4集", msg)

    def test_undo_skips_paused(self):
        entry = _entry(paused=True, watched_ep=5)
        ok, msg = _undo_watched_one(entry)
        self.assertFalse(ok)
        self.assertEqual(entry["watched_ep"], 5)
        self.assertIn("已停播，已跳过", msg)

    def test_set_watched_skips_paused(self):
        entry = _entry(paused=True, watched_ep=2)
        ok, msg = _set_watched_one(entry, 8)
        self.assertFalse(ok)
        self.assertEqual(entry["watched_ep"], 2)
        self.assertIn("已停播，已跳过", msg)

    def test_set_watched_active(self):
        entry = _entry(watched_ep=2)
        ok, _ = _set_watched_one(entry, 8)
        self.assertTrue(ok)
        self.assertEqual(entry["watched_ep"], 8)

    def test_progress_label_shows_paused(self):
        entry = _entry(paused=True, watched_ep=4)
        self.assertIn("停播", _progress_label(entry))
        self.assertNotIn("今日已看", _progress_label(entry))


if __name__ == "__main__":
    unittest.main()

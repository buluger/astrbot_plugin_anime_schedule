import unittest

from backlog_progress import (
    add_today,
    poster_caption,
    apply_emoji,
    cycle_today_count,
    format_today_push,
    is_finished,
    parse_upload_block,
    pick_suggestions,
    set_today_count,
    sink_finished,
    today_count,
    undo_emoji,
    undo_today,
    week_count,
)

TODAY = "2026-10-07"


def _entry(title="钢炼", watched=20, max_ep=64, logs=None):
    return {
        "title": title,
        "watched_ep": watched,
        "max_ep": max_ep,
        "logs": logs or [],
    }


class UploadParseTest(unittest.TestCase):
    def test_single_line_with_progress(self):
        parsed = parse_upload_block("钢之炼金术师 64 20")
        self.assertEqual(parsed, [("钢之炼金术师", 64, 20)])

    def test_batch_lines(self):
        parsed = parse_upload_block("钢炼 64 20\n进击的巨人 87")
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[1], ("进击的巨人", 87, None))

    def test_pipe_separator(self):
        parsed = parse_upload_block("钢炼 64 | 巨人 87 10")
        self.assertEqual([item[0] for item in parsed], ["钢炼", "巨人"])


class TodayCountTest(unittest.TestCase):
    def test_set_today_increases_total_and_week(self):
        entry = _entry(logs=[{"date": "2026-10-05", "count": 2}])
        ok, msg = set_today_count(entry, 2, TODAY)
        self.assertTrue(ok)
        self.assertEqual(entry["watched_ep"], 22)
        self.assertEqual(today_count(entry, TODAY), 2)
        self.assertEqual(week_count(entry, TODAY), 4)
        self.assertIn("今天补了 2 集", msg)

    def test_set_today_replaces_previous_today(self):
        entry = _entry(watched=22, logs=[{"date": TODAY, "count": 2}])
        ok, _ = set_today_count(entry, 1, TODAY)
        self.assertTrue(ok)
        self.assertEqual(entry["watched_ep"], 21)
        self.assertEqual(today_count(entry, TODAY), 1)

    def test_cannot_pass_max_ep(self):
        entry = _entry(watched=63, max_ep=64)
        ok, msg = set_today_count(entry, 3, TODAY)
        self.assertTrue(ok)
        self.assertEqual(entry["watched_ep"], 64)
        self.assertTrue(is_finished(entry))
        self.assertIn("已完结", msg)

    def test_add_and_undo(self):
        entry = _entry()
        add_today(entry, 1, TODAY)
        self.assertEqual(today_count(entry, TODAY), 1)
        ok, _ = undo_today(entry, TODAY)
        self.assertTrue(ok)
        self.assertEqual(entry["watched_ep"], 20)
        self.assertEqual(today_count(entry, TODAY), 0)

    def test_undo_empty_day_fails(self):
        ok, msg = undo_today(_entry(), TODAY)
        self.assertFalse(ok)
        self.assertIn("还没有补番记录", msg)

    def test_cycle_one_two_three(self):
        entry = _entry()
        cycle_today_count(entry, TODAY)
        cycle_today_count(entry, TODAY)
        cycle_today_count(entry, TODAY)
        self.assertEqual(today_count(entry, TODAY), 3)
        cycle_today_count(entry, TODAY)
        self.assertEqual(today_count(entry, TODAY), 1)
        self.assertEqual(entry["watched_ep"], 21)

    def test_poster_caption_includes_today(self):
        entry = _entry(logs=[{"date": TODAY, "count": 2}], watched=22)
        text, finished = poster_caption(entry, TODAY)
        self.assertFalse(finished)
        self.assertEqual(text, "今日+2·已看22/64")

    def test_poster_caption_finished(self):
        text, finished = poster_caption(_entry(watched=12, max_ep=12), TODAY)
        self.assertTrue(finished)
        self.assertEqual(text, "完结 12/12")

    def test_previous_week_not_counted(self):
        entry = _entry(logs=[{"date": "2026-09-30", "count": 4}])
        self.assertEqual(week_count(entry, TODAY), 0)


class SuggestionTest(unittest.TestCase):
    def test_fill_two_shows_when_one_airing(self):
        entries = [_entry("A"), _entry("B"), _entry("C")]
        picks = pick_suggestions(entries, airing_count=1)
        self.assertEqual([item[0] for item in picks], [1, 2])
        self.assertEqual([item[2] for item in picks], [1, 1])

    def test_one_show_absorbs_gap(self):
        picks = pick_suggestions([_entry("A"), _entry("完结", watched=12, max_ep=12)], 1)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0][2], 2)

    def test_full_day_hides_backlog(self):
        picks = pick_suggestions([_entry("A"), _entry("B")], airing_count=3)
        self.assertEqual(picks, [])
        self.assertIsNone(format_today_push([_entry("A")], 3, TODAY))

    def test_custom_quota_opens_a_slot(self):
        picks = pick_suggestions([_entry("A"), _entry("B")], airing_count=3, quota=4)
        self.assertEqual([item[0] for item in picks], [1])
        text = format_today_push([_entry("A"), _entry("B")], 3, TODAY, quota=4)
        self.assertIn("A", text or "")

    def test_finished_sinks(self):
        entries = sink_finished([
            _entry("完结", watched=12, max_ep=12),
            _entry("还在看"),
        ])
        self.assertEqual([e["title"] for e in entries], ["还在看", "完结"])

    def test_push_text_absent_without_active(self):
        text = format_today_push([_entry("完结", watched=12, max_ep=12)], 1, TODAY)
        self.assertIsNone(text)

    def test_single_emoji_sets_count(self):
        entries = [_entry()]
        entries, ok, msg = apply_emoji(entries, 2, airing_count=1, today=TODAY)
        self.assertTrue(ok)
        self.assertEqual(today_count(entries[0], TODAY), 2)
        self.assertIn("2 集", msg)

    def test_multi_emoji_cycles_selected_index(self):
        entries = [_entry("A"), _entry("B")]
        entries, ok, _ = apply_emoji(entries, 2, airing_count=1, today=TODAY)
        self.assertTrue(ok)
        self.assertEqual(today_count(entries[0], TODAY), 0)
        self.assertEqual(today_count(entries[1], TODAY), 1)
        entries, _, _ = apply_emoji(entries, 2, airing_count=1, today=TODAY)
        self.assertEqual(today_count(entries[1], TODAY), 2)

    def test_undo_emoji_steps_back_one(self):
        entries = [_entry()]
        entries, _, _ = apply_emoji(entries, 3, airing_count=1, today=TODAY)
        entries, ok, _ = undo_emoji(entries, 3, airing_count=1, today=TODAY)
        self.assertTrue(ok)
        self.assertEqual(today_count(entries[0], TODAY), 2)


if __name__ == "__main__":
    unittest.main()

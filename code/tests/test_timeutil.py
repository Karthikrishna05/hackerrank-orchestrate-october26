"""Tests for ghostaudit.timeutil (tools_spec T2: time handling)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from ghostaudit.timeutil import at_or_before, parse_human_date, parse_ts, to_iso

UTC = timezone.utc


class TestParseTs(unittest.TestCase):
    def test_parse_ts_accepts_z_suffix(self):
        self.assertEqual(parse_ts("2026-09-15T11:59:39Z"), datetime(2026, 9, 15, 11, 59, 39, tzinfo=UTC))

    def test_parse_ts_accepts_explicit_utc_offset(self):
        self.assertEqual(parse_ts("2026-09-15T11:59:39+00:00"), datetime(2026, 9, 15, 11, 59, 39, tzinfo=UTC))

    def test_parse_ts_accepts_space_separated_naive_form_as_utc(self):
        self.assertEqual(parse_ts("2026-09-15 11:59:39"), datetime(2026, 9, 15, 11, 59, 39, tzinfo=UTC))

    def test_parse_ts_converts_other_offsets_to_utc(self):
        got = parse_ts("2026-09-15T13:59:39+02:00")
        self.assertEqual(got, datetime(2026, 9, 15, 11, 59, 39, tzinfo=UTC))
        self.assertEqual(got.utcoffset(), timedelta(0))

    def test_parse_ts_keeps_fractional_seconds(self):
        self.assertEqual(parse_ts("2026-09-15T11:59:39.250Z").microsecond, 250000)

    def test_parse_ts_strips_surrounding_whitespace(self):
        self.assertEqual(parse_ts("  2026-09-15T11:59:39Z \n"), datetime(2026, 9, 15, 11, 59, 39, tzinfo=UTC))

    def test_parse_ts_always_returns_timezone_aware_utc(self):
        for text in ("2026-09-15T11:59:39Z", "2026-09-15 11:59:39", "2026-09-15T11:59:39-05:00"):
            with self.subTest(text=text):
                got = parse_ts(text)
                self.assertIsNotNone(got.tzinfo)
                self.assertEqual(got.utcoffset(), timedelta(0))

    def test_parse_ts_accepts_naive_datetime_as_utc(self):
        self.assertEqual(parse_ts(datetime(2026, 1, 1, 12, 0)), datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    def test_parse_ts_converts_aware_datetime_to_utc(self):
        cet = timezone(timedelta(hours=1))
        self.assertEqual(parse_ts(datetime(2026, 1, 1, 13, 0, tzinfo=cet)), datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    def test_parse_ts_raises_value_error_for_garbage(self):
        for bad in ("", "   ", "yesterday", "15/09/2026", "2026-13-01T00:00:00Z", "2026-09-15T25:00:00Z"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_ts(bad)

    def test_parse_ts_orders_mixed_formats_chronologically(self):
        stamps = ["2026-09-15T12:00:00+02:00",  # 10:00Z
                  "2026-09-15 11:00:00",        # 11:00Z
                  "2026-09-15T09:30:00Z",       # 09:30Z
                  "2026-09-15T10:30:00+00:00"]  # 10:30Z
        self.assertEqual(sorted(stamps, key=parse_ts),
                         ["2026-09-15T09:30:00Z", "2026-09-15T12:00:00+02:00",
                          "2026-09-15T10:30:00+00:00", "2026-09-15 11:00:00"])


class TestAtOrBefore(unittest.TestCase):
    def test_earlier_timestamp_is_at_or_before(self):
        self.assertTrue(at_or_before("2026-09-15T11:59:38Z", "2026-09-15T11:59:39Z"))

    def test_equal_timestamp_counts_as_at_or_before(self):
        # Spec T2: gold cites evidence_03, captured exactly at audit_at.
        self.assertTrue(at_or_before("2026-09-15T11:59:39Z", "2026-09-15T11:59:39Z"))

    def test_later_timestamp_is_not_at_or_before(self):
        self.assertFalse(at_or_before("2026-09-15T11:59:40Z", "2026-09-15T11:59:39Z"))

    def test_one_microsecond_late_is_not_at_or_before(self):
        self.assertFalse(at_or_before("2026-09-15T11:59:39.000001Z", "2026-09-15T11:59:39Z"))

    def test_equality_holds_across_formats(self):
        self.assertTrue(at_or_before("2026-09-15 11:59:39", "2026-09-15T11:59:39Z"))
        self.assertTrue(at_or_before("2026-09-15T11:59:39Z", "2026-09-15 11:59:39"))

    def test_equality_holds_across_offsets(self):
        self.assertTrue(at_or_before("2026-09-15T13:59:39+02:00", "2026-09-15T11:59:39Z"))
        self.assertTrue(at_or_before("2026-09-15T11:59:39Z", "2026-09-15T13:59:39+02:00"))

    def test_offset_is_applied_before_comparing(self):
        # As raw strings "13:00+02:00" > "12:00Z", but as instants 11:00Z < 12:00Z.
        self.assertTrue(at_or_before("2026-09-15T13:00:00+02:00", "2026-09-15T12:00:00Z"))

    def test_accepts_datetime_arguments(self):
        cutoff = datetime(2026, 9, 15, 11, 59, 39, tzinfo=UTC)
        self.assertTrue(at_or_before(cutoff, cutoff))
        self.assertTrue(at_or_before("2026-09-15T11:00:00Z", cutoff))


class TestToIso(unittest.TestCase):
    def test_to_iso_formats_utc_with_z_suffix_and_whole_seconds(self):
        self.assertEqual(to_iso("2026-09-15T13:59:39.900+02:00"), "2026-09-15T11:59:39Z")

    def test_to_iso_round_trips_through_parse_ts(self):
        for text in ("2026-09-15T11:59:39Z", "2024-02-29T00:00:00Z", "2026-12-31T23:59:59Z"):
            with self.subTest(text=text):
                self.assertEqual(to_iso(parse_ts(text)), text)
                self.assertEqual(parse_ts(to_iso(text)), parse_ts(text))


class TestParseHumanDate(unittest.TestCase):
    def test_parses_spec_example_with_hour_and_minute(self):
        self.assertEqual(parse_human_date("recorded 25 Aug 2026 04 03 UTC"),
                         datetime(2026, 8, 25, 4, 3, tzinfo=UTC))

    def test_parses_second_spec_example(self):
        self.assertEqual(parse_human_date("issued 05 Sep 2026 06 30 UTC"),
                         datetime(2026, 9, 5, 6, 30, tzinfo=UTC))

    def test_parses_pdftotext_layout_with_wide_gap_between_hour_and_minute(self):
        self.assertEqual(parse_human_date("submitted 18 Jun 2026 10     24 UTC"),
                         datetime(2026, 6, 18, 10, 24, tzinfo=UTC))

    def test_date_without_time_is_midnight_utc(self):
        self.assertEqual(parse_human_date("Retain until 28 May 2033 | AR"),
                         datetime(2033, 5, 28, tzinfo=UTC))

    def test_parses_colon_separated_time_with_seconds(self):
        self.assertEqual(parse_human_date("25 Aug 2026 04:03:09"), datetime(2026, 8, 25, 4, 3, 9, tzinfo=UTC))

    def test_parses_full_and_abbreviated_month_names_in_any_case(self):
        expected = datetime(2026, 9, 5, tzinfo=UTC)
        for text in ("5 September 2026", "5 Sept 2026", "5 SEP 2026", "05 sep 2026"):
            with self.subTest(text=text):
                self.assertEqual(parse_human_date(text), expected)

    def test_returns_the_first_date_when_several_are_present(self):
        self.assertEqual(parse_human_date("04 May 2026 | Retain until 04 May 2033"),
                         datetime(2026, 5, 4, tzinfo=UTC))

    def test_skips_an_impossible_date_and_uses_the_next_valid_one(self):
        self.assertEqual(parse_human_date("31 Feb 2026 then 1 Mar 2026"), datetime(2026, 3, 1, tzinfo=UTC))

    def test_returns_none_when_no_human_date_is_present(self):
        for text in ("", "no date here", "2026-08-25", "Total 12 items 2026", "12 Mayday 2026"):
            with self.subTest(text=text):
                self.assertIsNone(parse_human_date(text))

    def test_result_is_timezone_aware_utc(self):
        got = parse_human_date("25 Aug 2026 04 03 UTC")
        self.assertEqual(got.utcoffset(), timedelta(0))


if __name__ == "__main__":
    unittest.main()

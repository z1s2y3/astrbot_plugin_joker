"""Focused tests for portal table/list pagination semantics."""

from __future__ import annotations

import unittest

from web_client.pagination import paginate, pagination_metadata, page_numbers, parse_page


class PaginationTests(unittest.TestCase):
    def test_invalid_page_values_are_safe_and_one_based(self):
        self.assertEqual(1, parse_page(None))
        self.assertEqual(1, parse_page("not-a-number"))
        self.assertEqual(3, parse_page("3"))
        self.assertEqual(10_000, parse_page("999999"))

    def test_in_memory_pager_slices_without_dropping_total(self):
        result = paginate(list(range(1, 56)), page=2, page_size=25)
        self.assertEqual(list(range(26, 51)), result["items"])
        self.assertEqual(55, result["total"])
        self.assertEqual(3, result["page_count"])
        self.assertTrue(result["has_previous"])
        self.assertTrue(result["has_next"])

    def test_database_metadata_clamps_page_after_filtering(self):
        result = pagination_metadata(2, page=9, page_size=25)
        self.assertEqual(1, result["page"])
        self.assertEqual(1, result["page_count"])
        self.assertFalse(result["has_previous"])
        self.assertFalse(result["has_next"])

    def test_large_page_ranges_use_ellipsis_markers(self):
        links = page_numbers(50, 100)
        self.assertEqual(1, links[0])
        self.assertEqual(100, links[-1])
        self.assertIn(0, links)
        self.assertEqual(5, len([item for item in page_numbers(50, 100, max_links=5) if item]))


if __name__ == "__main__":
    unittest.main()

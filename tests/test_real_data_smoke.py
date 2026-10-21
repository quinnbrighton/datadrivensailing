"""Smoke checks against the real scraped database and data files.

These need the nightly pipeline's output, which is not in a fresh clone, so they are marked `needs_data` and skipped
there.  They guard the contracts the site generators rely on: the schools table that auto_links reads, and the women's
regatta list that womens_rules reads.
"""
import sqlite3
from pathlib import Path

import pytest

import womens_rules as wr

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.needs_data


def test_schools_table_has_names_and_unique_slugs():
    con = sqlite3.connect(f"file:{ROOT / 'db' / 'icsa.db'}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT school, slug FROM schools WHERE school <> '' AND slug <> ''").fetchall()
    finally:
        con.close()
    assert len(rows) > 100
    slugs = [slug for _school, slug in rows]
    assert len(set(slugs)) == len(slugs)
    assert all(slug == slug.lower() and " " not in slug for slug in slugs)


@pytest.mark.needs_data("data/women_regattas.csv")
def test_every_listed_women_regatta_is_recognised_as_womens():
    names = wr.load_women_regatta_names(str(ROOT / "data" / "women_regattas.csv"))
    assert names
    index = wr.build_index(names)
    assert all(wr.is_womens_regatta(n, names, index) for n in names)

"""Automatic school / season / conference links (auto_links.link_html).

Protects the rules in the module docstring: only whole-cell names become links, only when the target page exists, never
inside links, buttons or top headings, never to the page itself, relative to the page's depth, and idempotent.  A temporary
site tree stands in for website/ and the module's school map replaces the database.
"""
from pathlib import Path

import pytest

import auto_links as al

pytestmark = pytest.mark.unit

PAGE_HTML = "<table><tr><td>{}</td></tr></table>"


@pytest.fixture
def site(tmp_path: Path, monkeypatch) -> Path:
    web = tmp_path / "website"
    for rel in ("icsa/schools/boston-university", "icsa/f26", "icsa/conferences/neisa", "issa/schools/lakeside"):
        (web / rel).mkdir(parents=True)
        (web / rel / "index.html").write_text("<html></html>")
    monkeypatch.setattr(al, "WEB", str(web))
    monkeypatch.setattr(al, "_exists", {})
    monkeypatch.setattr(al, "_schools", {
        "icsa": {"Boston University": "boston-university", "Ghost School": "ghost-school"},
        "issa": {"Lakeside": "lakeside"},
    })
    return web


def page(site: Path, rel: str = "icsa/f26/some-regatta/index.html") -> str:
    return str(site / rel)


def link(site: Path, html: str, rel: str = "icsa/f26/some-regatta/index.html") -> str:
    return al.link_html(html, page(site, rel))


def test_whole_cell_school_becomes_a_relative_link(site):
    out = link(site, PAGE_HTML.format("Boston University"))
    assert '<td><a href="../../schools/boston-university/">Boston University</a></td>' in out


def test_link_depth_follows_the_page_location(site):
    out = link(site, PAGE_HTML.format("Boston University"), "icsa/index.html")
    assert 'href="schools/boston-university/"' in out


def test_school_without_a_page_is_left_alone(site):
    html = PAGE_HTML.format("Ghost School")
    assert link(site, html) == html


def test_names_inside_a_sentence_are_not_linked(site):
    html = "<p>Boston University won the regatta</p>"
    assert link(site, html) == html


def test_season_and_conference_cells_link_when_their_pages_exist(site):
    out = link(site, PAGE_HTML.format("Fall 2026"))
    assert 'href="../../f26/"' in out
    assert 'href="../../conferences/neisa/"' in link(site, PAGE_HTML.format("NEISA"))
    html = PAGE_HTML.format("Spring 2019")                # no such season page
    assert link(site, html) == html


@pytest.mark.parametrize("wrapper", [
    '<a href="/x">{}</a>', "<h1>{}</h1>", "<button>{}</button>", "<script>{}</script>",
    '<td class="totalrow">{}</td>', '<span onclick="go()">{}</span>', "<title>{}</title>",
])
def test_names_in_skipped_elements_are_not_linked(site, wrapper):
    html = wrapper.format("Boston University")
    assert link(site, html) == html


def test_a_page_does_not_link_to_itself(site):
    html = PAGE_HTML.format("Boston University")
    assert link(site, html, "icsa/schools/boston-university/index.html") == html


def test_other_league_school_links_across_leagues(site):
    assert 'href="../../../issa/schools/lakeside/"' in link(site, PAGE_HTML.format("Lakeside"))


def test_slash_separated_list_links_each_school(site):
    out = link(site, PAGE_HTML.format("Boston University / Lakeside"))
    assert out.count("<a href=") == 2
    assert "</a> / <a href=" in out


def test_podium_card_keeps_the_win_percentage_outside_the_link(site):
    out = link(site, "<div>Boston University &mdash; 82% win</div>")
    assert '">Boston University</a> &mdash; 82% win' in out


def test_entities_in_names_are_matched_unescaped(site, monkeypatch):
    monkeypatch.setitem(al._schools["icsa"], "Texas A&M", "texas-am")
    (site / "icsa/schools/texas-am").mkdir()
    (site / "icsa/schools/texas-am/index.html").write_text("")
    assert 'href="../../schools/texas-am/"' in link(site, PAGE_HTML.format("Texas A&amp;M"))


def test_link_to_a_missing_school_page_is_reduced_to_text(site):
    html = '<td><a href="../../schools/nowhere/">Nowhere U</a></td>'
    assert link(site, html) == "<td>Nowhere U</td>"


def test_existing_school_link_is_kept(site):
    html = '<td><a href="../../schools/boston-university/">BU</a></td>'
    assert link(site, html) == html


def test_pages_outside_the_league_folders_are_untouched(site):
    html = PAGE_HTML.format("Boston University")
    assert al.link_html(html, str(site / "other" / "index.html")) == html


def test_linking_is_idempotent(site):
    once = link(site, PAGE_HTML.format("Boston University") + PAGE_HTML.format("NEISA"))
    assert link(site, once) == once


def test_sweep_rewrites_only_pages_that_change(site):
    changed, unchanged = Path(page(site)), Path(page(site, "icsa/f26/other/index.html"))
    changed.parent.mkdir(parents=True), unchanged.parent.mkdir(parents=True)
    changed.write_text(PAGE_HTML.format("Boston University"))
    unchanged.write_text("<p>nothing to link</p>")
    assert al._sweep(str(changed)) == 1 and al._sweep(str(unchanged)) == 0
    assert al._sweep(str(changed)) == 0


def test_links_a_page_script_builds_by_concatenation_are_left_alone():
    """The rankings page builds school links in JavaScript ('<a href="../schools/'+slug+'/">'): they are not dead links."""
    import auto_links
    js = "var sc=s.slug?'<a href=\"../schools/'+s.slug+'/\">'+_esc(s.name)+'</a>':_esc(s.name);"
    assert auto_links._DEAD.sub(lambda m: m.group(2), js) == js

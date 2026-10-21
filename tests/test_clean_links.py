import pytest

from clean_links import clean


@pytest.mark.unit
@pytest.mark.parametrize('before, after', [
    ('<a href="f26/danmark/index.html">', '<a href="f26/danmark/">'),
    ('<a href="../index.html">', '<a href="../">'),
    ('<a href="index.html">', '<a href="./">'),
    ('<a href="/index.html">', '<a href="/">'),
    ("<a href='schools/mit/index.html'>", "<a href='schools/mit/'>"),
    ('<a href="../../boat-speed/index.html?host=MIT&boat=FJ">', '<a href="../../boat-speed/?host=MIT&boat=FJ">'),
    ('<a href="index.html#top">', '<a href="./#top">'),
    ('<form action="search/index.html">', '<form action="search/">'),
    ('<a href="https://datadrivensailing.com/icsa/index.html">', '<a href="https://datadrivensailing.com/icsa/">'),
    ('<a href="../../boat-speed/index.html?host=St. Mary\'s&boat=FJ">', '<a href="../../boat-speed/?host=St. Mary\'s&boat=FJ">'),
    # left alone
    ('<a href="https://scores.collegesailing.org/f26/index.html">', None),
    ('<a href="//cdn.example.com/x/index.html">', None),
    ('<a href="myindex.html">', None),
    ('<a href="index.html.bak">', None),
    ('<a href="about.html">', None),
    ('<script>var u = slug + "/index.html";</script>', None),
    ('<img src="index.html">', None),
])
def test_clean(before, after):
    assert clean(before) == (before if after is None else after)


@pytest.mark.unit
def test_clean_is_idempotent_and_keeps_unicode():
    page = '<a href="../é/index.html">Séb – ok</a><a href="index.html">x</a>'
    once = clean(page)
    assert once == '<a href="../é/">Séb – ok</a><a href="./">x</a>'
    assert clean(once) == once

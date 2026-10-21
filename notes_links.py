#!/usr/bin/env python3
"""A "From the notes" box on every generated page, pointing at the blog posts that explain what the page shows.

The generators call inject(html, path) from write(); the post list below is the only place the mapping lives.
Posts are chosen by the page's path and, for regatta pages, by what the page contains (team race, rotations tab).
"""
from __future__ import annotations

import re

# slug, title, one line, tags
POSTS = [
    ('what-a-rating-point-is-worth', 'What a rating point is worth', 'A one-point edge is a 73% chance to beat another boat in a race; two points is 88%.', {'sailor', 'fleet', 'rankings', 'school', 'home'}),
    ('how-skipper-ratings-work', 'How do skipper ratings work?', 'A log-strength fitted to every race at once, smoothed week to week.', {'sailor', 'rankings', 'methodology', 'home'}),
    ('why-not-elo', 'Why not Elo?', 'A joint fit lets every result inform every connected rating, both ways.', {'rankings', 'methodology'}),
    ('best-sailor-in-a', 'Should the best sailor sail A?', 'Add your two skippers’ ratings and compare with the field; above it the better skipper belongs in A.', {'school', 'fleet'}),
    ('how-team-race-forecasts-work', 'How team race forecasts work', 'Three boats’ ratings, a school term and a home edge; 78.8% of held-out matches called.', {'teamrace', 'rankings', 'methodology', 'home', 'school'}),
    ('what-a-crew-rating-does', 'How does a crew rating work?', 'A crew rating is small and uncertain; a worked example on one roster.', {'sailor', 'school'}),
    ('which-team-races-go-to-form', 'Which team races go to form?', 'Orderliness is a real property of a team race regatta and nothing known in advance predicts it.', {'teamrace', 'weather'}),
    ('venues-and-wind', 'Which venues get orderly in a breeze?', 'Some venues get orderly when it blows, a few get messier; the fleet comes first.', {'fleet', 'weather', 'home'}),
    ('are-the-boats-even', 'Are the boats even?', 'Boats differ by about 0.3 places a race; fleets are uneven from day one.', {'rotations', 'fleet', 'home'}),
    ('play-1-play-2-play-4', 'Play 1, Play 2, Play 4: how team races are won', 'How often each play happens, how that shifts with breeze and level, and whether a star wins differently.', {'teamrace', 'sailor_tr', 'school', 'home'}),
]
NEWEST_FIRST = ['play-1-play-2-play-4', 'are-the-boats-even', 'venues-and-wind', 'which-team-races-go-to-form', 'what-a-crew-rating-does',
                'how-team-race-forecasts-work', 'best-sailor-in-a', 'why-not-elo', 'how-skipper-ratings-work', 'what-a-rating-point-is-worth']
_BY_SLUG = {p[0]: p for p in POSTS}
_SKIP = ('/blog/', '/lab/', '/fantasy/', '/engineering/', '/recruiting-guide/', '/boat-speed/', '/dark/')


def pick(path: str, html: str) -> list[str]:
    """Slugs for this page, most relevant first (at most four)."""
    p = path.replace('\\', '/')
    if any(s in p for s in _SKIP) or not p.endswith('index.html'):
        return []
    rel = p.split('/website/', 1)[-1] if '/website/' in p else p
    parts = rel.split('/')
    league = parts[0] if parts and parts[0] in ('icsa', 'issa') else ''
    is_tr = ('Team race' in html or 'Team Race' in html) and ('tr-match' in html or 'Ratings &amp; Forecast' in html or 'team-race' in rel or 'Ratings & Forecast' in html)
    if league and len(parts) == 2:                                   # league homepage
        return NEWEST_FIRST[:4]
    if not league:
        return []
    section = parts[1] if len(parts) > 2 else ''
    if section == 'sailors' and len(parts) == 4:
        tags = ['sailor'] + (['sailor_tr'] if 'Team Race Rating' in html else [])
        want = ['how-skipper-ratings-work', 'what-a-rating-point-is-worth', 'what-a-crew-rating-does'] + (['play-1-play-2-play-4', 'how-team-race-forecasts-work'] if 'Team Race Rating' in html else [])
        return want[:4]
    if section == 'schools' and len(parts) >= 4:
        return ['best-sailor-in-a', 'how-team-race-forecasts-work', 'what-a-crew-rating-does', 'what-a-rating-point-is-worth']
    if section == 'rankings':
        return ['how-skipper-ratings-work', 'why-not-elo', 'how-team-race-forecasts-work', 'what-a-rating-point-is-worth']
    if section == 'methodology':
        return ['why-not-elo', 'how-skipper-ratings-work', 'how-team-race-forecasts-work', 'are-the-boats-even']
    if section == 'team-racing':
        return ['how-team-race-forecasts-work', 'which-team-races-go-to-form', 'play-1-play-2-play-4']
    if section == 'conferences' or section == 'seasons':
        return ['what-a-rating-point-is-worth', 'best-sailor-in-a', 'how-team-race-forecasts-work']
    if re.fullmatch(r'[fs]\d\d', section):
        if len(parts) == 3:                                           # season index
            return ['what-a-rating-point-is-worth', 'how-team-race-forecasts-work', 'best-sailor-in-a']
        if is_tr:
            return ['how-team-race-forecasts-work', 'play-1-play-2-play-4', 'which-team-races-go-to-form']
        want = ['what-a-rating-point-is-worth', 'best-sailor-in-a']
        if 'tab-rotations' in html:
            want.append('are-the-boats-even')
        want.append('venues-and-wind')
        return want[:4]
    return []


def box(slugs: list[str], heading: str = 'From the notes') -> str:
    if not slugs:
        return ''
    items = ''.join(f'<li><a href="/blog/{s}/">{_BY_SLUG[s][1]}</a><span>{_BY_SLUG[s][2]}</span></li>' for s in slugs if s in _BY_SLUG)
    return ('<div class="port notes-port"><h3 style="margin-bottom:0.3em">' + heading + '</h3>'
            '<p style="margin:0 0 0.4em;font-size:0.85em;color:#555">Short write-ups of what the ratings and results show, with the numbers and how they were computed.</p>'
            f'<ul class="notes-list">{items}</ul>'
            '<p style="margin:0.3em 0 0;font-size:0.85em"><a href="/blog/">All notes &rarr;</a></p></div>')


CSS = ('<style>.notes-port{border-top:1px solid #e1e0d9;margin-top:1.4em;padding-top:0.8em}'
       '.notes-list{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0.5em 1.2em}'
       '.notes-list li{font-size:0.9em;line-height:1.35}.notes-list li a{font-weight:bold;text-decoration:none}.notes-list li a:hover{text-decoration:underline}'
       '.notes-list li span{display:block;color:#555;font-size:0.92em}</style>')

_CONTENT_OPEN = '<div id="page-content">'
_TAG = re.compile(r'<div\b|</div>')


def _close_of_content(html: str) -> int:
    """Index of the </div> that closes #page-content, or -1."""
    i = html.find(_CONTENT_OPEN)
    if i < 0:
        return -1
    depth = 0
    for m in _TAG.finditer(html, i):
        depth += 1 if m.group(0) != '</div>' else -1
        if depth == 0:
            return m.start()
    return -1


def inject(html: str, path: str) -> str:
    if 'notes-port' in html:
        return html
    slugs = pick(path, html)
    if not slugs:
        return html
    block = box(slugs)
    j = _close_of_content(html)
    if j < 0:
        k = html.rfind('</body>')
        if k < 0:
            return html
        return html[:k] + '<div id="page-content">' + block + '</div>' + CSS + html[k:]
    return html[:j] + block + '\n' + html[j:].replace('</body>', CSS + '</body>', 1)

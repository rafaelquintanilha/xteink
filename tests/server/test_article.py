"""Article extraction (xteink/server/article.py): what is kept, what is dropped, and metadata."""

import pytest

from xteink.core.errors import CoreError
from xteink.server.article import ArticleError, extract

PARA = (
    "The quick brown fox jumps over the lazy dog while the reader waits patiently for "
    "something that looks like a real paragraph of prose to be measured. "
)

MODERN = f"""<!doctype html>
<html><head>
<title>A Careful Essay | Example Journal</title>
<meta property="og:title" content="A Careful Essay">
<meta name="author" content="By Ada Lovelace">
<style>p {{ color: red }}</style>
<script>window.tracker = "do-not-keep";</script>
</head><body>
<nav><a href="/">Home</a> <a href="/about">About us and many other things</a></nav>
<header class="masthead">Example Journal masthead text that is not the article</header>
<article>
  <h1>A Careful Essay</h1>
  <p>{PARA}First paragraph with a <a href="/ref?a=1&b=2">relative link</a>.</p>
  <h2>A section</h2>
  <p>{PARA}Second paragraph with <b>bold</b> and <i>italic</i> words &amp; an ampersand.</p>
  <img src="/pixel.gif" alt="tracking pixel">
  <ul><li>First point of the list</li><li><p>Second point, wrapped in a paragraph</p></li></ul>
  <blockquote><p>{PARA}A quoted passage.</p></blockquote>
  <pre>def f(x):
    return x &lt; 1</pre>
  <div class="share-buttons">Share this on every network you can think of</div>
  <p onclick="steal()">{PARA}Third paragraph. <a href="javascript:alert(1)">bad link</a></p>
</article>
<aside>Related: ten other stories you will not believe, each with a long title</aside>
<footer>Copyright Example Journal. All rights reserved worldwide forever.</footer>
</body></html>"""


def test_modern_article_keeps_the_text_and_drops_the_page_furniture():
    a = extract(MODERN, "https://example.org/essays/careful")
    assert a.title == "A Careful Essay"
    assert a.author == "Ada Lovelace"
    assert a.html.count("The quick brown fox") == 4
    assert "<h2>A section</h2>" in a.html
    assert "<strong>bold</strong>" in a.html and "<em>italic</em>" in a.html
    assert "words &amp; an ampersand" in a.html
    assert "<ul><li>First point of the list</li><li>Second point" in a.html
    assert "<blockquote><p>" in a.html
    assert "<pre><code>def f(x):\n    return x &lt; 1</code></pre>" in a.html
    for gone in (
        "tracker",
        "color: red",
        "About us",
        "masthead text",
        "Share this",
        "Related:",
        "Copyright",
        "<img",
        "onclick",
        "javascript:",
        "<script",
    ):
        assert gone not in a.html, gone
    # The page's own h1 repeats the title, which the EPUB metadata already carries.
    assert "<h1>" not in a.html


def test_links_are_made_absolute_and_only_http_links_survive():
    a = extract(MODERN, "https://example.org/essays/careful")
    assert '<a href="https://example.org/ref?a=1&amp;b=2">relative link</a>' in a.html
    assert "bad link" in a.html  # the text stays, the javascript: target does not
    assert 'Source: <a href="https://example.org/essays/careful">example.org</a>' in a.html


# A hand-written page in the old style: layout tables, <font>, no <p> at all.
OLD_STYLE = f"""<html><head><title>How to Think</title></head><body>
<table><tr><td><a href="index.html">Home</a><br><a href="essays.html">Essays</a></td>
<td><table><tr><td><font size="2" face="verdana">
<b>How to Think</b><br><br>
{PARA}One.<br><br>
{PARA}Two, with a line<br>break inside it.<br><br>
{PARA}Three.<br><br>
</font></td></tr></table></td></tr></table>
</body></html>"""


def test_old_style_page_with_line_breaks_becomes_paragraphs():
    a = extract(OLD_STYLE, "http://example.org/think.html")
    assert a.title == "How to Think"
    assert a.html.count("<p>") >= 4  # three paragraphs, then the source line
    assert "One.</p>" in a.html and "Three.</p>" in a.html
    assert "a line<br/>break inside it." in a.html
    assert "Essays" not in a.html  # the link column scores nothing
    assert "<table" not in a.html and "<font" not in a.html


def test_article_split_over_sibling_blocks_stays_whole():
    columns = "".join(
        f'<div class="col"><div><p>{PARA}Part {i}.</p><p>{PARA}More {i}.</p></div></div>'
        for i in range(5)
    )
    html = f"<html><body><main><article><section>{columns}</section></article></main></body></html>"
    a = extract(html, "https://example.org/x")
    for i in range(5):
        assert f"Part {i}." in a.html and f"More {i}." in a.html


def test_without_a_semantic_wrapper_similar_siblings_are_kept():
    html = (
        "<html><body><div id='page'>"
        f"<div class='a'><p>{PARA}Alpha.</p><p>{PARA}Beta.</p><p>{PARA}Gamma.</p></div>"
        f"<div class='b'><p>{PARA}Delta.</p><p>{PARA}Epsilon.</p></div>"
        "<div class='c'><a href='/1'>One</a> <a href='/2'>Two</a></div>"
        "</div></body></html>"
    )
    a = extract(html, "https://example.org/x")
    assert "Alpha." in a.html and "Epsilon." in a.html
    assert ">One<" not in a.html


def test_unclosed_paragraphs_and_list_items_are_separated():
    html = (
        f"<html><body><div><p>{PARA}One.<p>{PARA}Two."
        "<ul><li>a item<li>b item</ul></div></body></html>"
    )
    a = extract(html, "https://example.org/x")
    assert a.html.count("The quick brown fox") == 2
    assert "One.</p>" in a.html
    assert "<li>a item</li><li>b item</li>" in a.html


def test_hidden_elements_and_furniture_by_class_are_dropped_but_a_big_block_is_not():
    html = (
        "<html><body>"
        f"<div class='post-content has-sidebar'><p>{PARA}Kept one.</p><p>{PARA}Kept two.</p>"
        "<div hidden>Hidden text that must not appear anywhere</div>"
        "<div style='display: none'>Also hidden text here</div>"
        "<div id='comments'><p>A short comment from a reader of the page.</p></div>"
        "</div></body></html>"
    )
    a = extract(html, "https://example.org/x")
    assert "Kept one." in a.html and "Kept two." in a.html
    assert "Hidden text" not in a.html and "Also hidden" not in a.html
    assert "short comment" not in a.html


@pytest.mark.parametrize(
    "html",
    [
        "<html><head><title>App</title></head><body><div id='root'></div>"
        "<script>render()</script></body></html>",
        "<html><body><ul>"
        + "".join(
            f"<li><a href='/{i}'>Story number {i} with a long headline</a></li>" for i in range(40)
        )
        + "</ul></body></html>",
        "",
    ],
)
def test_page_without_an_article_is_refused(html):
    with pytest.raises(ArticleError) as exc:
        extract(html, "https://example.org/x")
    assert exc.value.code == "no_article"
    assert isinstance(exc.value, CoreError)


def test_title_falls_back_to_the_longest_part_of_the_page_title_then_the_url():
    body = f"<body><div><p>{PARA}{PARA}{PARA}</p></div></body>"
    a = extract(f"<html><head><title>Site | The Real Title Of It</title></head>{body}</html>")
    assert a.title == "The Real Title Of It"
    b = extract(f"<html>{body}</html>", "https://example.org/posts/my-post/")
    assert b.title == "my-post"
    assert b.author == ""


def test_markup_in_text_is_escaped():
    html = (
        f"<html><body><div><p>{PARA}{PARA}Use &lt;script&gt; tags &amp; 1 &lt; 2.</p></div>"
        "</body></html>"
    )
    a = extract(html, "https://example.org/x")
    assert "Use &lt;script&gt; tags &amp; 1 &lt; 2." in a.html


def test_boxes_that_are_mostly_links_are_dropped_but_a_linked_sentence_is_kept():
    languages = "".join(f"<li><a href='/{i}'>Language {i}</a></li>" for i in range(12))
    html = (
        "<html><body><main>"
        f"<ul class='interlanguage'>{languages}</ul>"
        f"<p>{PARA}Body one.</p><p>{PARA}Body two.</p>"
        "<p><a href='/paper'>Read the whole paper on the project site</a></p>"
        "<div><a href='/c1'>Category one</a> <a href='/c2'>Category two</a> "
        "<a href='/c3'>Category three</a></div>"
        "</main></body></html>"
    )
    a = extract(html, "https://example.org/x")
    assert "Body one." in a.html and "Body two." in a.html
    assert "Read the whole paper" in a.html
    assert "Language 3" not in a.html and "Category two" not in a.html


def test_site_stands_in_for_a_missing_author():
    body = f"<body><div><p>{PARA}{PARA}{PARA}</p></div></body>"
    head = "<head><meta property='og:site_name' content='The Example Review'></head>"
    named = extract(f"<html>{head}{body}</html>", "https://www.example.org/a")
    assert (named.author, named.site) == ("", "The Example Review")
    bare = extract(f"<html>{body}</html>", "https://www.example.org/a")
    assert bare.site == "example.org"
    assert extract(f"<html>{body}</html>").site == ""

"""The copilot's markdown becomes a clean, SAFE HTML email."""
from bankai import emailformat


def test_headings_and_emphasis_render():
    html = emailformat.render_email("Update", "# Big\n\nSome **bold** and *italic* text.")
    assert "<h1" in html and "Big" in html
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html


def test_a_pipe_table_becomes_a_real_table():
    md = (
        "Here is the calendar:\n\n"
        "| Date | What | Amount |\n"
        "|---|---|---|\n"
        "| Aug 27 | Ford's standing draw | $1,270 |\n"
        "| Aug 31 | Apple statement | $5,685 |\n"
    )
    html = emailformat.render_email("Calendar", md)
    assert "<table" in html and "</table>" in html
    assert "<th" in html and "Date" in html
    assert "Aug 27" in html and "$5,685" in html
    assert html.count("<tr") >= 3  # header + 2 body rows


def test_lists_render():
    html = emailformat.render_email("x", "- first\n- second\n\n1. one\n2. two")
    assert "<ul" in html and html.count("<li") == 4
    assert "<ol" in html


def test_raw_html_in_the_source_is_neutralized():
    # a hostile transaction memo carried into an email body must never become markup
    html = emailformat.render_email("x", "Payment to <script>alert(1)</script> Corp")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_links_render_but_only_http():
    html = emailformat.render_email("x", "See [the portal](https://claude.ai/billing) now.")
    assert '<a href="https://claude.ai/billing"' in html
    # a non-http scheme is not turned into a link
    plain = emailformat.render_email("x", "run [x](javascript:evil) please")
    assert "javascript:evil" not in plain or "<a" not in plain


def test_the_document_is_whole_and_branded():
    html = emailformat.render_email("Weekly", "body")
    assert html.startswith("<!DOCTYPE html>")
    assert "BankAI" in html and "household copilot" in html
    assert html.rstrip().endswith("</html>")


def test_callout_panels_render_with_their_colour():
    tip = emailformat.render_email("x", "> [!TIP]\n> Pay the Apple Card in full.")
    assert "#0f6b57" in tip and "Tip" in tip and "Pay the Apple Card" in tip
    warn = emailformat.render_email("x", "> [!WARNING] A late fee breaks the plan.")
    assert "Heads up" in warn and "late fee" in warn
    # an ordinary blockquote (no [!TYPE]) is still a blockquote, not a callout
    plain = emailformat.render_email("x", "> just a quote")
    assert "<blockquote" in plain


def test_an_unknown_callout_type_falls_back_to_a_blockquote():
    html = emailformat.render_email("x", "> [!BOGUS] hello")
    assert "<blockquote" in html and "hello" in html


def test_plain_prose_still_becomes_paragraphs():
    html = emailformat.render_email("x", "Just a normal sentence.\n\nAnd another one.")
    assert html.count("<p") == 2

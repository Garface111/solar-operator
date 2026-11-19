"""Email-safe charts, and their fenced-block rendering inside emails."""
from bankai import charts, emailformat


# --- the components ---

def test_bar_chart_sizes_bars_by_magnitude():
    html = charts.bar_chart("Groceries | $640\nDining | $320\nTransport | $160")
    assert "Groceries" in html and "$640" in html
    # the largest gets ~100% width, the half-value ~50%
    assert 'width="100%"' in html
    assert 'width="50%"' in html and 'width="25%"' in html
    # email-safe: tables + background, no svg/style block
    assert "<svg" not in html and "background:" in html


def test_bar_chart_accepts_a_custom_color_and_display():
    html = charts.bar_chart("Apple Card | 7600 | #dc2626 | -$7,600")
    assert "#dc2626" in html and "-$7,600" in html


def test_stat_cards_render_tiles():
    html = charts.stat_cards("$1.26M | Net worth\n$1,149 | Spent this week")
    assert "$1.26M" in html and "Net worth" in html
    assert "$1,149" in html and html.count("<td") >= 2


def test_progress_meter_computes_percent():
    html = charts.progress_bars("1420 payoff | 4800 | 16000")
    assert "1420 payoff" in html and "(30%)" in html  # 4800/16000 = 30%


def test_progress_handles_zero_target_without_crashing():
    html = charts.progress_bars("weird | 5 | 0")
    assert "(0%)" in html


def test_number_parsing_handles_suffixes_and_symbols():
    assert charts._num("$1,234.50") == 1234.5
    assert charts._num("1.2M") == 1_200_000.0
    assert charts._num("45%") == 45.0
    assert charts._num("nonsense") == 0.0


# --- inside an email ---

def test_a_barchart_fence_renders_as_a_chart_in_an_email():
    md = "Here's the breakdown:\n\n```barchart\nGroceries | 640\nDining | 320\n```\n\nThat's it."
    html = emailformat.render_email("Spending", md)
    assert "Groceries" in html and 'width=' in html
    assert "```" not in html  # the fence itself never leaks as text
    assert "<p" in html  # surrounding prose still renders


def test_a_stats_fence_renders_tiles():
    html = emailformat.render_email("x", "```stats\n$1.26M | Net worth\n```")
    assert "$1.26M" in html and "Net worth" in html


def test_an_unknown_fence_becomes_a_code_block_not_a_chart():
    html = emailformat.render_email("x", "```python\nprint(1)\n```")
    assert "<pre" in html and "print(1)" in html


def test_chart_labels_are_escaped():
    html = charts.bar_chart("<script>x</script> | 10")
    assert "<script>" not in html and "&lt;script&gt;" in html

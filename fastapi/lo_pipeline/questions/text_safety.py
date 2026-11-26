"""
Plain-text fields of the output (question name, lesson title and description, report texts) must not
carry HTML that a browser could run or render. (Question text and choices are shown as HTML by ECEH;
units_output.html_text escapes them instead, so nothing is removed from them.)

clean_text removes:
- <script> and <style> blocks with their content, and HTML comments,
- every tag that is a real HTML element (<b>, <img>, <a>, <iframe>, …),
- every tag with attributes (<x onclick="…">), whatever its name.
Other "<word>" pieces stay: in programming material they are code, e.g. "#include <stdio.h>",
"vector<int>", "Map<K, V>". Without attributes they cannot run anything even if shown as HTML.
Entities are decoded ("&lt;" -> "<"), so the text reads as plain text.

Hints are HTML on purpose (<p>…</p>); they are escaped and cleaned by nh3 in units_output.hint_html.
In the plain-text fields a name such as "The <p> tag" loses "<p>"; ECEH shows them as text, so
escaping there would show "&lt;p&gt;" instead.
"""
import html
import re

# HTML element names (HTML living standard, plus old ones browsers still render)
HTML_ELEMENTS = frozenset("""
a abbr acronym address applet area article aside audio b base basefont bdi bdo bgsound big blink
blockquote body br button canvas caption center cite code col colgroup data datalist dd del details
dfn dialog dir div dl dt em embed fieldset figcaption figure font footer form frame frameset h1 h2 h3
h4 h5 h6 head header hgroup hr html i iframe image img input ins isindex kbd keygen label legend li
link listing main map mark marquee math menu menuitem meta meter multicol nav nextid nobr noembed
noframes noscript object ol optgroup option output p param picture plaintext portal pre progress q rb
rp rt rtc ruby s samp script search section select slot small source spacer span strike strong style
sub summary sup svg table tbody td template textarea tfoot th thead time title tr track tt u ul var
video wbr xmp
""".split())

_BLOCK = re.compile(r"<\s*(script|style)\b[^>]*>.*?(<\s*/\s*\1\s*>|$)", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?(-->|$)", re.DOTALL)
# "<name ...>" or "</name>" with the name right after "<" (so "a < b and c > d" is not a tag)
_TAG = re.compile(r"<(/?)([A-Za-z][\w:.-]*)(\s[^<>]*)?/?>")


def _is_unsafe_tag(match: re.Match) -> bool:
    name, attributes = match.group(2).lower(), (match.group(3) or "").strip(" /")
    return name in HTML_ELEMENTS or bool(attributes)


def clean_text(text: str) -> str:
    """Plain text without HTML that could run or render (see the module docstring)."""
    if "<" not in text and "&" not in text:
        return text
    previous = None
    while previous != text:  # repeat: removing one tag can join pieces into a new one ("<scr<b>ipt>")
        previous = text
        text = _BLOCK.sub("", text)
        text = _COMMENT.sub("", text)
        text = _TAG.sub(lambda m: "" if _is_unsafe_tag(m) else m.group(0), text)
    text = html.unescape(text)
    # decoding may create a new tag ("&lt;img onerror=…&gt;"): clean once more, without decoding again
    if text != previous:
        text = _TAG.sub(lambda m: "" if _is_unsafe_tag(m) else m.group(0), _COMMENT.sub("", _BLOCK.sub("", text)))
    return text.strip()


def cut_at_word(text: str, limit: int) -> str:
    """Text shortened to at most `limit` characters at the end of a word ("…" at the end)."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:  # a word boundary not too far back
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"

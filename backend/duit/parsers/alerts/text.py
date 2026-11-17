"""An email's text as lines, the form alert templates match against.

Alerts are mostly HTML. The HTML becomes one line per block element, with " | " between table
cells, so "<tr><td>Amount</td><td>RM5.00</td></tr>" reads "Amount | RM5.00". Script, style and
head are dropped. Typographic quotes become ASCII, so a template writes "You've" either way.
"""

from email.message import EmailMessage
from html.parser import HTMLParser

BLOCK_TAGS = frozenset(
    "p div br tr li h1 h2 h3 h4 h5 h6 table tbody thead tfoot section header footer hr".split()  # noqa: SIM905
)
HIDDEN_TAGS = frozenset({"script", "style", "head", "title"})
QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "\xa0": " "})


class HtmlText(HTMLParser):
    def __init__(self, *, show_images: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        self.show_images = show_images

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in HIDDEN_TAGS:
            self.hidden += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "img" and self.show_images:
            alt = dict(attrs).get("alt")
            self.parts.append(f" [img alt={alt!r}] " if alt else " [img] ")

    def handle_endtag(self, tag: str) -> None:
        if tag in HIDDEN_TAGS:
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)

    def lines(self) -> list[str]:
        return clean("".join(self.parts).splitlines())


def clean(lines: list[str]) -> list[str]:
    out = [" ".join(line.translate(QUOTES).split()).strip(" |") for line in lines]
    return [line for line in out if line]


def html_lines(html: str, *, show_images: bool = False) -> list[str]:
    parser = HtmlText(show_images=show_images)
    parser.feed(html)
    parser.close()
    return parser.lines()


def body_lines(message: EmailMessage) -> list[str]:
    """The HTML part as text, or the plain-text part when there is no HTML."""
    part = message.get_body(preferencelist=("html", "plain"))
    if part is None:
        return []
    content = str(part.get_content())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    if part.get_content_type() == "text/html":
        return html_lines(content)
    return clean(content.splitlines())

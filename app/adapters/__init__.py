from .html_cards import HtmlCardsAdapter
from .railway_pdf import RailwayPdfAdapter
from .rss import RssAdapter


def get_adapter(source):
    kind = source.get("kind")
    if kind == "html":
        return HtmlCardsAdapter(source.get("parser", {}))
    if kind == "rss":
        return RssAdapter()
    if kind == "railway_pdf":
        return RailwayPdfAdapter(source.get("parser", {}))
    raise ValueError(f"unknown source kind {kind!r}")

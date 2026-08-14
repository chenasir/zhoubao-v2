"""Source normalization helpers."""
from __future__ import annotations

from urllib.parse import urlparse

from .config import load_source_aliases


def source_from_google_title(title: str) -> tuple[str, str]:
    """Split Google News title like 'Headline - Reuters' into headline/source."""
    if " - " not in title:
        return title.strip(), ""
    headline, source = title.rsplit(" - ", 1)
    if len(source) > 60:
        return title.strip(), ""
    return headline.strip() or title.strip(), source.strip()


def normalize_source_name(raw: str, url: str = "") -> str:
    aliases = load_source_aliases()
    source = (raw or "").strip()
    if source in aliases.get("aliases", {}):
        return aliases["aliases"][source]

    low = source.lower()
    if "bloomberg" in low:
        return "Bloomberg"
    if "reuters" in low:
        return "Reuters"
    if "zawya" in low:
        return "Zawya"
    if "arab news" in low:
        return "Arab News"
    if "saudigazette" in low or "saudi gazette" in low:
        return "Saudi Gazette"
    if "ked global" in low:
        return "KED Global"
    if "chosun" in low:
        return "Chosun Biz"
    if "yahoo" in low:
        return "Yahoo Finance"
    if "business korea" in low:
        return "Business Korea"
    if "pulse" in low or "maeil" in low:
        return "Pulse"
    if "qazinform" in low:
        return "Qazinform"
    if "kazakhstan stock exchange" in low or "қазақстан қор биржасы" in low:
        return "KASE"
    if "astana international exchange" in low:
        return "AIX"
    if "astana international financial centre" in low or "aifc" in low:
        return "AIFC"
    if "saudi press agency" in low:
        return "Saudi Press Agency"
    if "financial services commission" in low:
        return "Korea FSC"
    if "korea exchange" in low:
        return "Korea Exchange"

    host = urlparse(url or "").netloc.replace("www.", "")
    if host:
        host_name = host.split(":")[0]
        if host_name in aliases.get("aliases", {}):
            return aliases["aliases"][host_name]
        return host_name.split(".")[0].replace("-", " ").title()
    return source


def source_homepage(source: str, url: str = "") -> str:
    homepages = load_source_aliases().get("homepages", {})
    canonical = normalize_source_name(source, url=url)
    if canonical in homepages:
        return homepages[canonical]
    if url:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/"
    return ""

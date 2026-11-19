"""AIRA's answers about a web page in NEOX's side panel, grounded in the page's own text.

The model may answer only from the page and must quote it ("..."). The quotes are then checked in
code against the page text: a quote that is not on the page is flagged, and an answer with no
quote is labelled unverified (the same rule as AIRA's citations, layer 8).
"""
from __future__ import annotations

import re

MAX_PAGE_CHARS = 12000
SYSTEM = """You are AIRA, the assistant built into the NEOX browser. Answer the user's question using ONLY
the web page below. Support every claim with a short exact quote from the page in double quotes,
like: The price is "$40 per month". If the page does not say, answer "I'm not sure, this page doesn't
say." Never add facts from memory. Keep it short; use "- " bullets for lists.

PAGE: {title}
URL: {url}
---
{text}
---"""

QUOTE_RE = re.compile(r'"([^"\n]{6,300})"|“([^”\n]{6,300})”')


def messages(question: str, page: dict) -> list[dict]:
    text = page.get("text", "")[:MAX_PAGE_CHARS]
    return [{"role": "system", "content": SYSTEM.format(title=page.get("title", ""), url=page.get("url", ""), text=text)},
            {"role": "user", "content": question}]


def retry_prompt(check: dict) -> str:
    missing = "; ".join(f'"{q}"' for q in check["unverified"])
    return (f"These quotes are not on the page word for word: {missing}. Answer again, copying every quote "
            "exactly as it appears on the page (same words), or say you're not sure.")


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def check(answer: str, page_text: str) -> dict:
    """{'status': verified | unverified | not sure, 'quotes': [...], 'unverified': [quotes not on the page]}"""
    page = _norm(page_text)
    quotes = [a or b for a, b in QUOTE_RE.findall(answer)]
    missing = [q for q in quotes if _norm(q).strip(" .,;:") not in page]
    not_sure = re.search(r"\bI['’]m not sure\b", answer, re.I) is not None
    if missing:
        status = "unverified"
    elif quotes:
        status = "verified"
    elif not_sure:
        status = "not sure"
    else:
        status = "unverified"
    return {"status": status, "quotes": quotes, "unverified": missing}

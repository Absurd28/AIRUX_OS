"""Grounding layer 8: research answers cite local files or URLs; sentences no source supports are
flagged; answers with no source are labelled "unverified".

A sentence is supported by a source passage when at least SUPPORT_MIN of its content words occur
in the passage AND every number in the sentence occurs there with the same word after it (so
"95%" is not supported by "85%", and "512 GPUs" is not supported by "512 tokens ... 8 GPUs"). Sentences that cite sources ([1], [2]) are
checked against those sources only; uncited sentences against all of them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SUPPORT_MIN = 0.5
STOP = set("""a an the and or but if then than that this these those is are was were be been being of in on at to
for from by with without as into over under about between it its it's they them their there here we our you your
he she his her i me my not no do does did done can could would should will may might must also very more most
such which who whom whose what when where why how all any each both few many much some other only own same so too
just has have had having per via using use used""".split())
CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
NUM = re.compile(r"\d+(?:[.,]\d+)?")
# a number and the word it counts: "512 GPUs", "512-token", "85.2% accuracy", "6 days"
NUM_WORD = re.compile(r"(\d+(?:[.,]\d+)?)\s*%?[\s\-]*([A-Za-z]+)?")


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def content_words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z][a-z\-]+", text.lower()) if len(w) > 2 and w not in STOP}


def numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUM.findall(text)}


def number_pairs(text: str) -> set[tuple[str, str | None]]:
    """(number, stem of the word right after it, or None)."""
    out = set()
    for num, word in NUM_WORD.findall(text):
        w = word.lower() if word else ""
        out.add((num.replace(",", ""), _stem(w) if w and w not in STOP else None))
    return out


def numbers_supported(sentence: str, passage: str) -> bool:
    """Every number in the sentence occurs in the passage with the same word after it
    (so "6 days on 512 GPUs" is not supported by "512 tokens ... 8 GPUs")."""
    have = number_pairs(passage)
    have_nums = {n for n, _ in have}
    for num, word in number_pairs(sentence):
        if word is None:
            if num not in have_nums:
                return False
        elif (num, word) not in have:
            return False
    return True


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])", text.strip())
    return [p.strip() for p in parts if p.strip()]


def passages(text: str, size: int = 3) -> list[str]:
    """Overlapping windows of `size` sentences, so a claim spanning two sentences still matches."""
    ss = sentences(text) or [text]
    return [" ".join(ss[i:i + size]) for i in range(0, max(1, len(ss) - size + 1))] or [text]


@dataclass
class Source:
    id: int
    ref: str        # local path or URL
    text: str

    def __post_init__(self):
        self._passages = passages(self.text)

    def best_support(self, sentence: str) -> tuple[float, bool, str]:
        words = content_words(sentence)
        best = (0.0, False, "")
        for p in self._passages:
            pw = content_words(p)
            cover = len(words & pw) / len(words) if words else 0.0
            nums_ok = numbers_supported(sentence, p)
            if (cover, nums_ok) > best[:2]:
                best = (cover, nums_ok, p)
        return best


@dataclass
class CheckedSentence:
    text: str
    cited: list[int]
    supported: bool
    coverage: float
    numbers_ok: bool
    source: str = ""


@dataclass
class CheckedAnswer:
    sentences: list[CheckedSentence] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)

    @property
    def unsupported(self) -> list[CheckedSentence]:
        return [s for s in self.sentences if not s.supported]

    @property
    def label(self) -> str:
        if not self.sources:
            return "unverified"
        return "verified" if not self.unsupported else "partly unverified"

    def render(self) -> str:
        if not self.sources:
            return "Unverified (no source): " + " ".join(s.text for s in self.sentences)
        body = " ".join(s.text + ("" if s.supported else " [unsupported]") for s in self.sentences)
        refs = "; ".join(f"[{s.id}] {s.ref}" for s in self.sources)
        return f"{body}\nSources: {refs}"


def check(answer: str, sources: list[Source]) -> CheckedAnswer:
    out = CheckedAnswer(sources=sources)
    by_id = {s.id: s for s in sources}
    for sent in sentences(answer):
        cited = [int(x) for m in CITE.findall(sent) for x in m.split(",")]
        pool = [by_id[c] for c in cited if c in by_id] or sources
        clean = CITE.sub("", sent).strip()
        cands = []
        for src in pool:
            cov, nums_ok, _ = src.best_support(clean)
            cands.append((cov >= SUPPORT_MIN and nums_ok, cov, nums_ok, src.ref))
        supported, cov, nums_ok, ref = max(cands) if cands else (False, 0.0, False, "")
        out.sentences.append(CheckedSentence(sent, cited, supported, round(cov, 2), nums_ok, ref))
    return out

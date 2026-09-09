"""Apply a names/books glossary to transcripts — orthography only, never guessing.

The dangerous idea is fuzzy matching. Measured on 6.8M chars of this sheikh's
v1 transcripts, a 0.80 similarity threshold proposes `الذي` (3,571 hits) -> `الذهبي`,
`ابن جريج` -> `ابن حجر` and `ابن عيينة` -> `ابن عثيمين`. The last two are distinct
real scholars. So this module only ever collapses *spellings of the same string*
(`ابن تيميه` -> `ابن تيمية`) and refuses anything it cannot reach by normalization.

Two outputs, both driven by the same glossary:
  apply()          rewrite a transcript's orthography  (safe, auditable)
  meili_synonyms() feed the search index instead of rewriting  (safest, reversible)
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

# The `lexical_normalized` profile from cohere-transcribe/docs/benchmarks.md, so
# glossary and transcript meet in the space the WER numbers were measured in.
_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ؤ": "و", "ئ": "ي", "ى": "ي", "ی": "ي",
    "ک": "ك", "پ": "ب", "ڤ": "ف",
    "ء": "", "ـ": "",
})
_WORD = re.compile(r"[؀-ۿ]+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_FOLD)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # ponytail: word-final ة -> ه only. A name spelled either way is the same name;
    # doing it mid-word would merge unrelated words. Drop this line if a collision
    # ever shows up in the report `apply` prints.
    text = re.sub(r"ة\b", "ه", text)
    return " ".join(text.split())


def load(path: str | Path) -> dict[str, list[str]]:
    """`{canonical: [variant, ...]}` from .json (list or object) or .txt.

    A .txt line is either a bare term, or `canonical = variant, variant`.
    """
    path = Path(path)
    if path.suffix == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k: list(v) for k, v in raw.items()} if isinstance(raw, dict) else {t: [] for t in raw}
    terms: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        canonical, _, variants = line.partition("=")
        terms[canonical.strip()] = [v.strip() for v in variants.split(",") if v.strip()]
    return terms


def alias_map(terms: dict[str, list[str]]) -> dict[str, str]:
    """normalized spelling -> canonical spelling. Collisions are an integrity error."""
    aliases: dict[str, str] = {}
    for canonical, variants in terms.items():
        for spelling in (canonical, *variants):
            key = normalize(spelling)
            if aliases.setdefault(key, canonical) != canonical:
                raise ValueError(
                    f"glossary collision: {key!r} claimed by "
                    f"{aliases[key]!r} and {canonical!r}"
                )
    return aliases


def apply(text: str, aliases: dict[str, str]) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite spellings that normalize onto a glossary entry. Returns (text, changes).

    Longest match wins, so `ابن القيم` is never split into `ابن` + `القيم`.
    """
    max_words = max((len(k.split()) for k in aliases), default=1)
    words = _WORD.findall(text)
    changes: list[tuple[str, str]] = []
    out, i = [], 0
    while i < len(words):
        for n in range(min(max_words, len(words) - i), 0, -1):
            span = words[i : i + n]
            canonical = aliases.get(normalize(" ".join(span)))
            if canonical is not None:
                original = " ".join(span)
                if original != canonical:
                    changes.append((original, canonical))
                out.append(canonical)
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return " ".join(out), changes


def meili_synonyms(terms: dict[str, list[str]]) -> dict[str, list[str]]:
    """Meilisearch `synonyms` setting: every spelling finds every other.

    Meilisearch only consults synonyms for queries of 1-3 words, so a longer
    honorific form (`شيخ الاسلام ابن تيمية`) never triggers and is dropped.
    """
    out: dict[str, list[str]] = {}
    for canonical, variants in terms.items():
        group = [canonical, *variants]
        for spelling in group:
            if len(spelling.split()) <= 3:
                out[spelling] = [s for s in group if s != spelling][:50]
    return out


def demo() -> None:
    terms = {
        "ابن تيمية": ["ابن تيميه", "بن تيمية"],
        "أبو داود": ["ابو داوود", "أبو داوود"],
        "ابن حجر": [],
        "الذهبي": [],
        "ابن القيم": [],
    }
    aliases = alias_map(terms)

    text, changes = apply("قال ابن تيميه رحمه الله وروى ابو داوود", aliases)
    assert text == "قال ابن تيمية رحمه الله وروى أبو داود", text
    assert changes == [("ابن تيميه", "ابن تيمية"), ("ابو داوود", "أبو داود")], changes

    # The whole point: near-miss words and distinct scholars are left alone.
    for untouched in ("الذي", "المذهب", "ابن جريج", "ابن عيينة", "الاصبهاني"):
        got, _ = apply(untouched, aliases)
        assert got == untouched, f"{untouched} was rewritten to {got}"

    # Longest match wins over the `ابن` prefix.
    got, _ = apply("ابن القيم", aliases)
    assert got == "ابن القيم", got

    try:
        alias_map({"ابن حجر": [], "ابن حجر العسقلاني": ["ابن حجر"]})
    except ValueError:
        pass
    else:
        raise AssertionError("collision not detected")

    assert "ابن تيميه" in meili_synonyms(terms)
    print("glossary demo ok")


if __name__ == "__main__":
    demo()

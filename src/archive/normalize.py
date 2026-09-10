"""The normalization contract (plan §6 Phase 4, `normVersion`).

One function, used by three callers that must never disagree: the `normalized*`
fields written into Convex, query preprocessing at search time, and the §8.4
query-log replay. `glossary.normalize` is a different contract for a different
job (orthography folding for term matching) and is deliberately left alone —
`docs/search_meilisearch_bge_m3_plan.md` forbids conflating the two.
"""

import re
import unicodedata

NORM_VERSION = "norm-v1"

# أ/إ/آ/ٱ → ا, ة → ه, ى → ي, tatweel dropped, Arabic-Indic digits → Latin.
_FOLD = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ة": "ه",
        "ى": "ي",
        "ـ": "",
        **{chr(0x0660 + n): str(n) for n in range(10)},  # ٠-٩
        **{chr(0x06F0 + n): str(n) for n in range(10)},  # ۰-۹ (extended)
    }
)


def normalize(text: str) -> str:
    """Plan §6 `normVersion` v1. Raw text is always stored separately for display."""
    text = unicodedata.normalize("NFKC", text)
    # Tashkeel and every other combining mark. NFKC leaves them attached.
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.translate(_FOLD).split())


def demo() -> None:
    assert normalize("الدَّرْسُ الأوَّل") == "الدرس الاول"
    assert normalize("الحلقة ٢٣") == "الحلقه 23"
    assert normalize("  شرح   كتـــاب  ") == "شرح كتاب"
    assert normalize("مُصْطَفَى") == "مصطفي"
    assert normalize("إسلام آمن ٱهدنا") == "اسلام امن اهدنا"
    print("normalize ok")


if __name__ == "__main__":
    demo()

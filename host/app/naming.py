"""Prompt-to-title and filename helpers for generated images."""

from __future__ import annotations

import re
import unicodedata

DEFAULT_IMAGE_TITLE = "AI Wallpaper"
DEFAULT_FILENAME_SLUG = "ai-wallpaper"
MAX_TITLE_WORDS = 8
MAX_TITLE_CHARS = 64
MAX_SLUG_CHARS = 48

_SMALL_TITLE_WORDS = frozenset(
    {"a", "an", "and", "at", "by", "for", "from", "in", "into", "of", "on", "or", "over", "the", "to", "under", "with"}
)
_TRAILING_CONNECTOR_WORDS = _SMALL_TITLE_WORDS | frozenset({"across", "as", "featuring", "made", "onto", "through"})
_LEADING_ARTICLE_RE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
_EXPLICIT_TITLE_RE = re.compile(r"^(?:image\s+)?title\s*:\s*([^\n]+)", re.IGNORECASE)
_CLI_FLAGS_RE = re.compile(r"\s--[a-zA-Z][a-zA-Z0-9_-]*(?:\s+(?!--)[a-zA-Z0-9.:,_-]+)?")
_QUOTES_RE = re.compile(r"""^[\s"'“”‘’`\[\]()]+|[\s"'“”‘’`\[\]().,;:!?-]+$""")

_LEADING_WRAPPER_RE = re.compile(
    r"^(?:(?:please\s+)?(?:create|generate|make|draw|render|design|paint|show(?:\s+me)?)\s+)?"
    r"(?:an?\s+|the\s+)?"
    r"(?:(?:ultra|hyper|super|very|high|detailed|vertical|portrait|mobile|phone|minimal|minimalist|"
    r"cinematic|stunning|beautiful|vibrant|dark|neon|aesthetic|glowing|luxury|clean|sharp|hd|uhd|4k|8k)[-\s]+)*"
    r"(?:live\s+)?(?:lock\s*screen\s+)?(?:wallpaper|background|image|photo(?:graph)?|picture|illustration|artwork|scene|render|lock\s*screen)\s+"
    r"(?:of|featuring|showing|depicting|with)\s+",
    re.IGNORECASE,
)

_FRAMING_CLAUSE_RE = re.compile(
    r"\b(?:aspect\s+ratio|lock\s*screen|depth\s+effect|negative\s+space|upper\s+third|lower\s+third|"
    r"depth\s+of\s+field|bokeh|shallow\s+focus|studio\s+light(?:ing)?|cinematic\s+lighting|"
    r"volumetric\s+lighting|photorealistic|hyperrealistic|ultra[-\s]*(?:detailed|sharp|realistic)|"
    r"high[-\s]*detail|national\s+geographic|documentary\s+photography|masterpiece|best\s+quality|"
    r"sharp\s+focus|trending\s+on|shot\s+on|clean\s+uninhabited|no\s+tourists|f/\d+(?:\.\d+)?|"
    r"\d+\s*:\s*\d+(?:\.\d+)?|4k|8k|uhd|fhd)\b",
    re.IGNORECASE,
)

_SHOT_PREFIX_RE = re.compile(
    r"^(?:(?:ultra\s+)?(?:wide|close[-\s]*up|macro|aerial|top[-\s]*down|overhead|low[-\s]*angle|high[-\s]*angle|"
    r"full|medium|portrait|cinematic)\s+(?:shot|view|angle|frame|composition))\b$",
    re.IGNORECASE,
)


_PURE_META_WORD_RE = re.compile(
    r"\b(?:ultra|hyper|super|high|detail|detailed|vertical|portrait|mobile|phone|wallpaper|background|"
    r"composition|resolution|aspect|ratio|lock\s*screen|depth|effect|negative|space|upper|lower|third|"
    r"foreground|midground|photorealistic|hyperrealistic|cinematic|sharp|clean|smooth|natural|"
    r"realistic|studio|lighting|light|bokeh|focus|shot|view|angle|frame|ios|4k|8k|hd|uhd|fhd|"
    r"with|and|for|of|the|a|an|in|on|to|no)\b",
    re.IGNORECASE,
)


def _is_meta_framing_segment(segment: str) -> bool:
    """Return True when a whole sentence/clause only describes camera/wallpaper framing specs."""
    cleaned = _QUOTES_RE.sub("", segment).strip()
    if not cleaned:
        return True
    if _SHOT_PREFIX_RE.match(cleaned):
        return True
    remaining = _FRAMING_CLAUSE_RE.sub(" ", cleaned)
    remaining = _PURE_META_WORD_RE.sub(" ", remaining)
    remaining = re.sub(r"[^A-Za-z]+", " ", remaining).strip()
    return len(remaining.split()) == 0


def _format_title_word(word: str, is_first: bool, is_last: bool) -> str:
    if "-" in word and not word.startswith("-") and not word.endswith("-"):
        parts = word.split("-")
        return "-".join(
            _format_title_word(part, is_first=is_first and idx == 0, is_last=is_last and idx == len(parts) - 1)
            for idx, part in enumerate(parts)
        )
    stripped = re.sub(r"[^A-Za-z0-9]", "", word)
    if stripped.isupper() and 2 <= len(stripped) <= 5:
        return word
    if re.match(r"^i(?:OS|Phone|Pad)$", word):
        return word
    lower = word.lower()
    if not is_first and not is_last and lower in _SMALL_TITLE_WORDS:
        return lower
    return lower[:1].upper() + lower[1:]


def _apply_title_case(text: str) -> str:
    tokens = text.split()
    if not tokens:
        return DEFAULT_IMAGE_TITLE
    return " ".join(
        _format_title_word(token, is_first=(idx == 0), is_last=(idx == len(tokens) - 1))
        for idx, token in enumerate(tokens)
    )


def _trim_to_title_bounds(text: str) -> str:
    words = text.split()
    if not words:
        return ""

    truncated = False
    if len(words) > MAX_TITLE_WORDS:
        words = words[:MAX_TITLE_WORDS]
        truncated = True

    selected: list[str] = []
    char_count = 0
    for word in words:
        next_len = len(word) if not selected else char_count + 1 + len(word)
        if next_len > MAX_TITLE_CHARS:
            truncated = True
            break
        selected.append(word)
        char_count = next_len

    if not selected:
        return words[0][:MAX_TITLE_CHARS].rstrip(",.;:-")

    if truncated:
        while len(selected) > 1 and selected[-1].rstrip(",.;:-").lower() in _TRAILING_CONNECTOR_WORDS:
            selected.pop()

    return " ".join(selected).rstrip(",.;:-")


def extract_image_title(prompt: str) -> str:
    """Extract a concise, human-readable image title from a generation prompt."""
    normalized = re.sub(r"\s+", " ", prompt).strip()
    if not normalized:
        return DEFAULT_IMAGE_TITLE

    explicit_match = _EXPLICIT_TITLE_RE.match(prompt.strip())
    if explicit_match:
        candidate = _QUOTES_RE.sub("", re.sub(r"\s+", " ", explicit_match.group(1)).strip())
        bounded = _trim_to_title_bounds(candidate)
        if bounded:
            return _apply_title_case(bounded)

    without_flags = _CLI_FLAGS_RE.sub("", normalized).strip()
    segments = [seg.strip() for seg in re.split(r"(?:\.\s+|;\s+|\s+\|\s+|\s+--\s+)", without_flags) if seg.strip()]
    if not segments:
        segments = [without_flags]

    chosen_segment = segments[0]
    for seg in segments:
        if not _is_meta_framing_segment(seg):
            chosen_segment = seg
            break

    wrapped = _LEADING_WRAPPER_RE.sub("", chosen_segment).strip()
    if wrapped and len(wrapped.split()) >= 1:
        chosen_segment = wrapped

    comma_parts = [_QUOTES_RE.sub("", part).strip() for part in chosen_segment.split(",") if _QUOTES_RE.sub("", part).strip()]
    kept_parts: list[str] = []
    for part in comma_parts:
        if not kept_parts:
            if len(comma_parts) > 1 and (_SHOT_PREFIX_RE.match(part) or _is_meta_framing_segment(part)):
                continue
            kept_parts.append(part)
            continue

        if _is_meta_framing_segment(part):
            break

        joined_words = " ".join(kept_parts).split()
        part_words = part.split()
        joined_text = f"{', '.join(kept_parts)}, {part}"
        if len(joined_words) <= 3 and len(joined_words) + len(part_words) <= 7 and len(joined_text) <= MAX_TITLE_CHARS:
            kept_parts.append(part)
        else:
            break

    subject = ", ".join(kept_parts) if kept_parts else _QUOTES_RE.sub("", chosen_segment).strip()
    subject = _QUOTES_RE.sub("", subject).strip()
    without_article = _LEADING_ARTICLE_RE.sub("", subject).strip()
    if without_article and len(subject.split()) >= 2:
        subject = without_article

    bounded = _trim_to_title_bounds(subject)
    if not bounded:
        fallback_words = _trim_to_title_bounds(_QUOTES_RE.sub("", without_flags).strip())
        if not fallback_words:
            return DEFAULT_IMAGE_TITLE
        return _apply_title_case(fallback_words)

    return _apply_title_case(bounded)


def title_to_filename_slug(title: str, max_length: int = MAX_SLUG_CHARS) -> str:
    """Convert an extracted image title into a URL-safe and header-safe lowercase slug."""
    ascii_text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if not slug:
        return DEFAULT_FILENAME_SLUG
    if len(slug) <= max_length:
        return slug

    clipped = slug[:max_length]
    if "-" in clipped:
        word_boundary = clipped.rsplit("-", 1)[0]
        if len(word_boundary) >= 12:
            return word_boundary.strip("-") or DEFAULT_FILENAME_SLUG
    return clipped.strip("-") or DEFAULT_FILENAME_SLUG


def safe_header_value(value: str, fallback: str = DEFAULT_IMAGE_TITLE) -> str:
    """Return a clean printable ASCII HTTP header value without control characters."""
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", ascii_text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or fallback

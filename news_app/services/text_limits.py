import re


def enforce_word_limit(text: str, max_words: int) -> str:
    cleaned = " ".join(text.replace('"', "").replace("'", "").split())
    words = cleaned.split()
    if len(words) <= max_words:
        return cleaned
    return " ".join(words[:max_words]).rstrip(".,;:")


def enforce_summary_limit(text: str, max_words: int = 200) -> str:
    cleaned = " ".join(text.split())
    cleaned = re.sub(r"^(summary|answer)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    words = cleaned.split()
    if len(words) <= max_words:
        return cleaned
    return " ".join(words[:max_words]).rstrip(".,;:")

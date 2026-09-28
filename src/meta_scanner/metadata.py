"""Clearly marked extractive preview while a local LLM is unavailable."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

from .extractors import Extraction, TextBlock

STOP_WORDS = {"これ", "それ", "ため", "こと", "もの", "場合", "情報", "内容", "ページ", "資料", "する", "いる"}


def _keywords(blocks: list[TextBlock]) -> list[str]:
    from sudachipy import dictionary, tokenizer

    parser = dictionary.Dictionary().create()
    counts: Counter[str] = Counter()
    # Bound keyword work independently of the extraction size limit.
    budget = 250_000
    for block in blocks:
        if budget <= 0:
            break
        fragment = block.text[:budget]
        budget -= len(fragment)
        for token in parser.tokenize(fragment, tokenizer.Tokenizer.SplitMode.C):
            term = token.surface()
            if token.part_of_speech()[0] == "名詞" and 2 <= len(term) <= 40 and term not in STOP_WORDS:
                counts[term] += 1
    return [word for word, _ in counts.most_common(10)]


def _language(blocks: list[TextBlock]) -> str:
    sample = "".join(block.text[:1000] for block in blocks[:20])
    if not sample:
        return "und"
    japanese = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    if japanese and latin and min(japanese, latin) / max(japanese, latin) >= 0.3:
        return "mixed"
    return "ja" if japanese else "en" if latin else "und"


def _quote(block: TextBlock) -> str:
    return block.text.replace("\n", " ")[:300]


def preview_metadata(extraction: Extraction, filename: str) -> dict:
    blocks = extraction.blocks
    warnings = list(dict.fromkeys([*extraction.warnings, "LOCAL_LLM_PENDING", "EXTRACTIVE_PREVIEW"]))
    title = (blocks[0].text.splitlines()[0][:80] if blocks else filename[:80]) or "無題"
    # A preview quotes source text; it is not presented as an AI summary.
    first = blocks[0].text if blocks else ""
    last = blocks[-1].text if len(blocks) > 1 else ""
    summary = (first[:300] + (" … " + last[:250] if last else ""))[:600] or None
    keywords = _keywords(blocks) if blocks else []
    evidence = []
    if blocks:
        evidence.append({"field": "title", "locator": blocks[0].locator, "quote": _quote(blocks[0])})
        evidence.append({"field": "summary", "locator": blocks[0].locator, "quote": _quote(blocks[0])})
        if last:
            evidence.append({"field": "summary", "locator": blocks[-1].locator, "quote": _quote(blocks[-1])})
        for word in keywords:
            source = next((block for block in blocks if word in block.text), None)
            if source:
                offset = source.text.find(word)
                evidence.append({"field": "keywords", "locator": source.locator,
                                 "quote": source.text[max(0, offset - 60):offset + len(word) + 60].replace("\n", " ")[:300]})
    return {
        "status": "review_required",
        "title": title,
        "title_source": "document" if blocks else "filename",
        "summary": summary,
        "keywords": keywords,
        "language": _language(blocks),
        "extraction_methods": list(dict.fromkeys(block.method for block in blocks)) or ["native"],
        "generation_method": "extractive_fallback",
        "evidence": evidence,
        "warnings": warnings,
        "model_sha256": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

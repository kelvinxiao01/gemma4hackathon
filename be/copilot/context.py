"""Turn retrieval hits into numbered excerpts the model can cite by index.

The model never writes a URL or a page number. It writes an excerpt number, and
the citation is looked up here, so a citation cannot name a document the
retrieval did not actually return.
"""

QUOTE_CHARS = 300


def build(hits: list) -> tuple[str, list[dict]]:
    """Return the excerpt block and the parallel citation list.

    Citations are 1-based to match what the prompt asks the model to emit.
    """
    blocks, citations = [], []
    for n, hit in enumerate(hits, 1):
        # `page_label` is None on html rows, where the document URL is the only
        # locator the source offers.
        ref = hit.page_label or hit.doc_url
        blocks.append(f"[{n}] {hit.payer_name} {hit.plan_type} {ref}\n"
                      f"{hit.text}")
        citations.append({
            "payer_name": hit.payer_name, "plan_type": hit.plan_type,
            "ref": ref, "doc_url": hit.doc_url,
            "quote": " ".join(hit.text.split())[:QUOTE_CHARS],
        })
    return "\n\n".join(blocks), citations


def resolve(index, citations: list[dict]) -> dict:
    """Map a model-written excerpt number onto a real citation.

    Out of range falls back to the first excerpt rather than raising: a wrong
    citation number is a presentation defect, and losing the whole
    determination over one would be worse.
    """
    if not citations:
        return {}
    try:
        position = int(index) - 1
    except (TypeError, ValueError):
        position = -1
    if not 0 <= position < len(citations):
        print(f"[copilot error] citation {index!r} out of range, using 1")
        position = 0
    return citations[position]

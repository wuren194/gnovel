"""Exact quotation assembly shared by review and prose-citation requests."""
from __future__ import annotations

from .structure_store import StructureError


def quote_span(text: str, quote: dict) -> dict:
    if not isinstance(quote, dict) or set(quote) - {'text', 'start', 'end', 'page'}:
        raise StructureError('EVIDENCE_QUOTE_INVALID', 'quote requires text and optional exact bounds')
    value = quote.get('text')
    if not isinstance(value, str) or not value.strip():
        raise StructureError('EVIDENCE_QUOTE_INVALID', 'quote must be nonempty text')
    if 'page' in quote:
        from .artifacts import sha256_json
        page = quote['page']
        if (not isinstance(page, dict) or set(page) != {'start', 'end', 'chunkHash'}
                or type(page['start']) is not int or type(page['end']) is not int
                or not 0 <= page['start'] < page['end'] <= len(text)
                or 'start' in quote or 'end' in quote):
            raise StructureError('EVIDENCE_QUOTE_INVALID', 'select a bound page and quote text without manual offsets')
        start, end = page['start'], page['end']
        window = text[start:end]
        if page['chunkHash'] != sha256_json({'text': window, 'start': start, 'end': end}):
            raise StructureError('EVIDENCE_QUOTE_MISMATCH', 'page differs from bound text')
        local = quote_span(window, {'text': value})
        return {'start': start + local['start'], 'end': start + local['end'], 'text': value}
    if 'start' in quote or 'end' in quote:
        start, end = quote.get('start'), quote.get('end')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text) or text[start:end] != value:
            raise StructureError('EVIDENCE_QUOTE_MISMATCH', 'quote does not match the bound text')
    else:
        start = text.find(value)
        if start < 0:
            raise StructureError('EVIDENCE_QUOTE_MISSING', 'quote is absent from the bound text')
        if text.find(value, start + 1) >= 0:
            raise StructureError('EVIDENCE_QUOTE_AMBIGUOUS', 'quote occurs more than once; provide exact bounds')
        end = start + len(value)
    return {'start': start, 'end': end, 'text': value}

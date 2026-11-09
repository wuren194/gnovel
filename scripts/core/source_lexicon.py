"""Read an explicitly selected project word list; never infer bans from prose."""
from __future__ import annotations

import json
import re

from .structure_store import StructureError
from .write_sets import digest, safe_project_path


def scan_terms(root, *, terms=None, termsPath=None, termsFormat='json', startLine=None, endLine=None):
    if termsPath is None:
        if termsFormat != 'json' or startLine is not None or endLine is not None:
            raise StructureError('SOURCE_LEXICON_INVALID', 'word-list options require termsPath')
        return terms, None
    if terms is not None:
        raise StructureError('SOURCE_LEXICON_INVALID', 'choose terms or a project termsPath')
    path = safe_project_path(root, termsPath)
    if termsFormat not in {'json', 'lines', 'quoted'}:
        raise StructureError('SOURCE_LEXICON_INVALID', 'termsFormat must be json, lines or quoted')
    try:
        data = path.read_bytes()
        text = data.decode('utf-8')
    except (OSError, UnicodeError) as exc:
        raise StructureError('SOURCE_LEXICON_INVALID', 'project word list is unreadable', {'path': termsPath}) from exc
    lines = text.splitlines()
    if startLine is not None or endLine is not None:
        if type(startLine) is not int or type(endLine) is not int or not 1 <= startLine <= endLine <= len(lines):
            raise StructureError('SOURCE_LEXICON_INVALID', 'line selection requires valid inclusive startLine and endLine')
        text = '\n'.join(lines[startLine-1:endLine])
    if termsFormat == 'quoted' and (startLine is None or endLine is None):
        raise StructureError('SOURCE_LEXICON_INVALID', 'quoted lists require explicit startLine and endLine')
    if termsFormat == 'json':
        try:
            words = json.loads(text)
        except ValueError as exc:
            raise StructureError('SOURCE_LEXICON_INVALID', 'JSON word list must be an array of strings') from exc
    elif termsFormat == 'lines':
        words = [line.strip() for line in text.splitlines() if line.strip()]
    else:
        words = re.findall(r'「([^「」\r\n]+)」', text)
    if not isinstance(words, list) or not words or any(not isinstance(word, str) or not word.strip() for word in words):
        raise StructureError('SOURCE_LEXICON_INVALID', 'selected word list must contain nonempty literal strings')
    return sorted(set(words)), {'path': str(path.relative_to(root.resolve())), 'sha256': digest(data),
                                'format': termsFormat, 'startLine': startLine, 'endLine': endLine,
                                'termCount': len(set(words)), 'semanticInterpretation': False}

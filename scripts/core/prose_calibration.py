"""Non-gating surface measurements; short paragraphs are not inferred defects."""
import importlib
import re
from statistics import pvariance


def calibrate(text):
    module = importlib.import_module('scripts.sepia_bridge' if __package__.startswith('scripts.') else 'sepia_bridge')
    paragraphs = []
    for match in re.finditer(r'\S[^\n]*(?:\n(?!\s*\n)[^\n]+)*', text):
        metrics = module.analyze_zh_calibration(match.group())
        lengths = [len(s) for s in module.split_sentences(match.group())]
        paragraphs.append({'start': match.start(), 'end': match.end(),
                           'line': text.count('\n', 0, match.start()) + 1,
                           'characters': len(match.group()),
                           'hanCharacters': len(re.findall(r'[\u4e00-\u9fff]', match.group())),
                           'sentenceCount': metrics['sentence_count'],
                           'sentenceLengthVariance': round(pvariance(lengths), 2) if lengths else 0.0})
    return {'advisory': True, 'affectsPassed': False,
            'sepia': module.analyze_zh_calibration(text), 'paragraphs': paragraphs}

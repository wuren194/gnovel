"""Original, temporary reference data for public retrieval tests."""
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import tempfile
from unittest.mock import patch


@contextmanager
def staged_reference(*, canonical=False):
    from scripts.core import knowledge_store
    from scripts.core.project import TOOLBOX_ROOT
    root = knowledge_store.TOOLBOX_ROOT / 'knowledge'
    root.mkdir(parents=True, exist_ok=True)
    directory_context = (nullcontext(knowledge_store.ASSETS) if canonical
                         else tempfile.TemporaryDirectory(dir=root))
    with directory_context as directory:
        assets = Path(directory).resolve()
        assets.mkdir(parents=True, exist_ok=True)
        sample = json.loads((TOOLBOX_ROOT / 'knowledge/example.json').read_text())
        sample['metadata'].update(assetId='KN-RELATIONSHIP-TRANSITION', title='关系推进',
                                  intents=['relationship_transition'], tags=['关系推进'])
        path = assets / 'KN-RELATIONSHIP-TRANSITION.md'
        with path.open('x', encoding='utf-8') as output:
            output.write('---\n' + json.dumps(sample['metadata'], ensure_ascii=False) +
                         '\n---\n\n# 方法\n先核对既有承诺，再通过共同完成的小事改变关系，留下可观察的行动与后果。\n')
        try:
            with (nullcontext() if canonical else patch.object(knowledge_store, 'ASSETS', assets)):
                yield assets
        finally:
            if canonical:
                path.unlink()

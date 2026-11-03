import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "alice.epub"


@pytest.fixture(scope="session")
def epub_path():
    if not FIXTURE.exists():
        from tests.fixtures.build_fixture import build

        build(FIXTURE)
    return FIXTURE


@pytest.fixture(scope="session")
def book(epub_path):
    from nobookends_sync.epub import load_epub

    return load_epub(epub_path)

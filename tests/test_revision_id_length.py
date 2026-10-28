"""alembic_version.version_num is VARCHAR(32): a longer revision id applies its DDL and then fails the
version stamp (po1 hit this on the Railway dev migrate run, 2026-10-28)."""
import pathlib
import re


def test_every_revision_id_fits_alembic_version():
    versions = pathlib.Path(__file__).parent.parent / "alembic/versions"
    too_long = []
    for path in versions.glob("*.py"):
        m = re.search(r'^revision\s*=\s*["\']([^"\']+)["\']', path.read_text(), re.M)
        if m and len(m.group(1)) > 32:
            too_long.append(m.group(1))
    assert not too_long, too_long

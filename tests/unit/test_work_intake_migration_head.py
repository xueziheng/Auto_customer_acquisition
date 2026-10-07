"""迁移链必须只有一个 Alembic head。"""

from itertools import pairwise
from pathlib import Path
from shutil import copytree

from alembic.config import Config
from alembic.script import ScriptDirectory

from infra.db.migration_hygiene import remove_appledouble_version_sidecars


def test_migration_chain_has_single_head(tmp_path: Path) -> None:
    clean_scripts = tmp_path / "migrations"
    copytree(
        "migrations",
        clean_scripts,
        ignore=lambda _directory, names: [
            name for name in names if name.startswith("._") or name == "__pycache__"
        ],
    )
    remove_appledouble_version_sidecars(clean_scripts / "versions")
    config = Config()
    config.set_main_option("script_location", str(clean_scripts))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()

    assert len(heads) == 1
    chain = list(scripts.walk_revisions(base="base", head=heads[0]))
    assert "0040" in {revision.revision for revision in chain[1:]}
    assert chain[0].revision == heads[0]
    assert chain[-1].down_revision is None
    assert len({revision.revision for revision in chain}) == len(chain)
    assert all(
        newer.down_revision == older.revision
        for newer, older in pairwise(chain)
    )
    assert {revision.revision for revision in scripts.walk_revisions()} == {
        revision.revision for revision in chain
    }

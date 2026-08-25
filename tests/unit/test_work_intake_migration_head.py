"""迁移链必须只有一个 Alembic head。"""

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
    heads = ScriptDirectory.from_config(config).get_heads()

    assert heads == ["0038"]

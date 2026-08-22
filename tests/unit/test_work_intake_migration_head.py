"""员工工作版本表必须属于唯一 Alembic head。"""

from pathlib import Path
from shutil import copytree

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_work_intake_migration_is_the_single_head(tmp_path: Path) -> None:
    clean_scripts = tmp_path / "migrations"
    copytree(
        "migrations",
        clean_scripts,
        ignore=lambda _directory, names: [
            name for name in names if name.startswith("._") or name == "__pycache__"
        ],
    )
    config = Config()
    config.set_main_option("script_location", str(clean_scripts))
    heads = ScriptDirectory.from_config(config).get_heads()

    assert heads == ["0029"]

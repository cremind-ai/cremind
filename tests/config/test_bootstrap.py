"""Reading the file that decides which database Cremind boots on.

Two of these are about one bad afternoon. A native Windows install wrote
``bootstrap.toml`` through PowerShell 5.1's ``Set-Content -Encoding utf8``,
which prefixes a UTF-8 BOM; ``toml`` reads that BOM as part of the first
comment's ``#`` and rejects the file. The fallback that exists precisely so a
malformed bootstrap "never bricks the server" then raised ``AttributeError``
instead — its logger was the module, not loguru — and every boot, every
``cremind db upgrade`` and every ``cremind db current`` died in the same place.

So: the reader tolerates a BOM, and the fallback survives being taken.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from app.config import bootstrap
from app.config.settings import BaseConfig


# The exact text both installers write for a fresh SQLite install.
INSTALLER_BOOTSTRAP = (
    "# Database selection. SQLite is the recommended default for native\n"
    '# installs; switch to "postgres" via the setup wizard if you want a\n'
    "# multi-process or networked DB.\n"
    'db_provider = "sqlite"\n'
)


@pytest.fixture
def system_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    monkeypatch.delenv("CREMIND_DB_PROVIDER", raising=False)
    return tmp_path


def test_a_missing_file_is_sqlite_and_not_a_commitment(system_dir):
    assert bootstrap.read_bootstrap()["db_provider"] == "sqlite"
    assert bootstrap.bootstrap_exists() is False


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_the_installers_bootstrap_parses_with_or_without_a_bom(system_dir, encoding):
    """``utf-8-sig`` here is what PowerShell 5.1 leaves on disk.

    The BOM lands immediately before the leading ``#``, which is why the error
    it produced named a character nobody wrote: "invalid character in key
    name: '#'".
    """
    (system_dir / "bootstrap.toml").write_text(INSTALLER_BOOTSTRAP, encoding=encoding)

    assert bootstrap.read_bootstrap()["db_provider"] == "sqlite"


def test_a_bom_does_not_lose_the_postgres_credentials(system_dir):
    """The quieter half of the same bug: a file that fails to parse reads as
    SQLite, so an install would come up on an empty local database instead of
    its own Postgres."""
    (system_dir / "bootstrap.toml").write_text(
        'db_provider = "postgres"\n\n[postgres]\nhost = "db.internal"\n'
        'password = "s3cret"\nport = 6543\n',
        encoding="utf-8-sig",
    )

    resolved = bootstrap.read_bootstrap()

    assert resolved["db_provider"] == "postgres"
    assert resolved["postgres"]["password"] == "s3cret"
    assert resolved["postgres"]["port"] == 6543


def test_a_malformed_file_is_refused_rather_than_guessed_at(system_dir):
    """The only other answer available here is the SQLite default.

    For a Postgres install that would boot onto a brand-new empty database
    beside the real one — no profiles, no conversations, indistinguishable from
    total data loss. Refusing is recoverable; that is not. What must never
    happen is the third option, which is what used to happen: the fallback's own
    logging raising AttributeError and taking the boot down with a stack trace
    that named neither the file nor the problem.
    """
    (system_dir / "bootstrap.toml").write_text("db_provider = [unterminated\n", encoding="utf-8")

    with pytest.raises(RuntimeError) as refused:
        bootstrap.read_bootstrap()

    message = str(refused.value)
    assert "bootstrap.toml" in message
    # Every way out, because nothing is serving yet to explain it later.
    assert "Setup Wizard" in message and "CREMIND_DB_PROVIDER" in message


def test_the_environment_can_override_a_file_it_cannot_read(system_dir, monkeypatch):
    """The escape hatch the refusal names has to actually work — and
    ``resolve_bootstrap`` applies the override only *after* reading the file."""
    from app.utils.logger import logger

    (system_dir / "bootstrap.toml").write_text("db_provider = [unterminated\n", encoding="utf-8")
    monkeypatch.setenv("CREMIND_DB_PROVIDER", "postgres")
    monkeypatch.setenv("CREMIND_POSTGRES_HOST", "db.internal")
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        resolved = bootstrap.resolve_bootstrap()
    finally:
        logger.remove(sink)

    assert resolved["db_provider"] == "postgres"
    assert resolved["postgres"]["host"] == "db.internal"
    assert any("bootstrap.toml is unreadable" in message for message in messages)


def test_the_logger_here_is_loguru_under_the_real_boot_import_order():
    """A fresh interpreter, because this can only go wrong in one direction.

    ``app/utils/logger.py`` logs at import time; that fires the bus sink, which
    pulls in app.events → app.storage → app.databases.factory → this module,
    all while ``app.utils`` is still half-initialised and has no ``logger``
    attribute yet. ``from app.utils import logger`` therefore bound the
    *module*, and the warning above raised AttributeError. Under pytest the
    import order is the other way round, so nothing in-process can catch it.
    """
    code = (
        "import sys\n"
        "import app.utils.logger\n"  # the boot order, not the test order
        "import app.config.bootstrap as bootstrap\n"
        "from loguru import logger\n"
        "print('chain', 'app.databases.factory' in sys.modules)\n"
        "print('bound', bootstrap.logger is logger)\n"
    )
    with tempfile.TemporaryDirectory() as isolated:
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "DISABLE_LOG": "true", "CREMIND_SYSTEM_DIR": isolated},
            capture_output=True, text=True, timeout=300,
        )

    assert result.returncode == 0, result.stderr
    # Without this the test could pass vacuously: the bus sink swallows every
    # exception, so an import chain that broke early would look like success.
    assert "chain True" in result.stdout, result.stdout
    assert "bound True" in result.stdout, result.stdout

from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Makes documents_converter importable regardless of the current working
# directory `alembic` is invoked from -- prepend_sys_path in alembic.ini
# only adds ".", which is only correct if run from the repo root.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from documents_converter.api import config as app_config  # noqa: E402
from documents_converter.api.db import Base  # noqa: E402
from documents_converter.api import models  # noqa: E402,F401 -- registers JobRecord on Base.metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Always migrate whatever database the app itself is actually configured
# to use (config.DATABASE_URL, same env var) rather than whatever's
# hardcoded in alembic.ini -- so `alembic upgrade head` run by hand and
# the app's own auto-migration at startup (see api/app.py) never
# disagree about which database they mean.
config.set_main_option("sqlalchemy.url", app_config.DATABASE_URL)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
#
# disable_existing_loggers=False is deliberate, not the fileConfig()
# default -- confirmed the hard way (see tests/conftest.py's
# _migrated_test_database fixture docstring): fileConfig()'s default
# (True) disables every already-configured logger not explicitly listed
# in alembic.ini's [loggers] section, which silently killed
# documents_converter.audit (api/audit.py) every time a migration ran,
# since that logger is naturally not something alembic.ini knows about.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Drives autogenerate (`alembic revision --autogenerate`) -- models.py's
# import above is what actually registers JobRecord onto this metadata.
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

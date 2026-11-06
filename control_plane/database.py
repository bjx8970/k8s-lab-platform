from sqlalchemy import create_engine

from .config import RuntimeSettings


def create_database_engine(settings: RuntimeSettings):
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"timeout": settings.connect_timeout_seconds},
    )

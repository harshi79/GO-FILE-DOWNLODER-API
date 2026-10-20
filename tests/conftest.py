import pytest

from gofile_bot.config import Settings
from gofile_bot.telegram.rich import reset_rich_support


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bot_token="123456:TEST-TOKEN",
        gofile_api_token=None,
        gofile_wt_salt="12af056dacea0b",
    )


@pytest.fixture(autouse=True)
def _reset_rich_support():
    """Keep the process-wide rich-message probe from leaking between tests."""
    reset_rich_support()
    yield
    reset_rich_support()

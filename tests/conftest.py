import pytest

from gofile_bot.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bot_token="123456:TEST-TOKEN",
        gofile_api_token=None,
        gofile_wt_salt="12af056dacea0b",
    )

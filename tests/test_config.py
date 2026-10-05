import pytest
from cryptography.fernet import Fernet
from nanny.config import Config


def environment():
    return {"DISCORD_TOKEN": "test-token", "NANNY_ENCRYPTION_KEY": Fernet.generate_key().decode()}


def test_valid_settings():
    config = Config.from_env(environment())
    assert config.database == "data/chronicle.db"
    assert config.guild_id is None


@pytest.mark.parametrize("key,value", [("DISCORD_TOKEN", ""), ("NANNY_ENCRYPTION_KEY", ""),
    ("NANNY_ENCRYPTION_KEY", "bad"), ("DISCORD_GUILD_ID", "-1"),
    ("NANNY_DATABASE", "database.db"), ("NANNY_DATABASE", "")])
def test_reject_invalid_settings_without_echoing_values(key, value):
    env = environment()
    env[key] = value
    with pytest.raises(ValueError) as error:
        Config.from_env(env)
    assert "test-token" not in str(error.value)


def test_test_guild():
    assert Config.from_env({**environment(), "DISCORD_GUILD_ID": "123"}).guild_id == 123


def test_config_repr_omits_secrets():
    env = environment()
    rendered = repr(Config.from_env(env))
    assert env["DISCORD_TOKEN"] not in rendered
    assert env["NANNY_ENCRYPTION_KEY"] not in rendered

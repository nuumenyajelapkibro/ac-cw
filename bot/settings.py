from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    TELEGRAM_TOKEN: str
    ORCH_URL: str = "https://api.yumini.ru"
    BASE_URL: str = "https://bot.yumini.ru"
    ENV: str = "prod"

    class Config:
        env_file = ".env"

settings = Settings()
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # local backend
    host: str = os.getenv("APP_HOST", "127.0.0.1")
    port: int = int(os.getenv("APP_PORT", "8091"))

    # backward compatibility only
    yandex_direct_use_sandbox: bool = os.getenv("YANDEX_DIRECT_USE_SANDBOX", "false").lower() == "true"

    # Yandex Direct API auth
    yandex_direct_oauth_token: str = os.getenv("YANDEX_DIRECT_OAUTH_TOKEN", "")
    yandex_direct_client_login: str = os.getenv("YANDEX_DIRECT_CLIENT_LOGIN", "")
    yandex_direct_accept_language: str = os.getenv("YANDEX_DIRECT_ACCEPT_LANGUAGE", "ru")

    @property
    def yandex_direct_sandbox_base_url(self) -> str:
        return "https://api-sandbox.direct.yandex.com/json/v5"

    @property
    def yandex_direct_production_base_url(self) -> str:
        return "https://api.direct.yandex.com/json/v5"

    @property
    def yandex_direct_base_url(self) -> str:
        """
        Backward-compatible default URL.
        New code should prefer explicit target routing instead of this property.
        """
        if self.yandex_direct_use_sandbox:
            return self.yandex_direct_sandbox_base_url
        return self.yandex_direct_production_base_url

    @property
    def is_yandex_direct_configured(self) -> bool:
        return bool(self.yandex_direct_oauth_token.strip())


settings = Settings()
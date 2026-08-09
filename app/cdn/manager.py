from typing import Dict, Type, Optional
from app.cdn.base import CDNProvider
from app.cdn.hackclub import HackClubCDNProvider
from app.models import CDNAccount

class CDNManager:
    """Registry and factory for dynamic CDN provider instances."""

    _PROVIDERS: Dict[str, Type[CDNProvider]] = {
        'Hack Club CDN': HackClubCDNProvider,
        'hackclub': HackClubCDNProvider
    }

    @classmethod
    def register_provider(cls, name: str, provider_cls: Type[CDNProvider]):
        cls._PROVIDERS[name] = provider_cls

    @classmethod
    def get_provider_instance(cls, account: CDNAccount) -> CDNProvider:
        provider_cls = cls._PROVIDERS.get(account.provider, HackClubCDNProvider)
        api_key = account.get_api_key()
        return provider_cls(api_key=api_key)

    @classmethod
    def test_account(cls, account: CDNAccount):
        provider = cls.get_provider_instance(account)
        return provider.test_connection()

    @classmethod
    def get_available_accounts(cls):
        """Return all enabled CDN accounts with current storage info."""
        accounts = CDNAccount.query.filter_by(enabled=True).all()
        result = []
        for acc in accounts:
            info = acc.to_dict(include_storage=True)
            result.append(info)
        return result

from src.config import settings
from src.otokens import privy


def test_privy_user_cache_purges_expired_and_evicts_oldest(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "privy_user_cache_seconds", 10)
    monkeypatch.setattr(privy, "MAX_PRIVY_USER_CACHE_ENTRIES", 2)
    privy._user_cache.clear()
    try:
        privy._user_cache.update(
            {
                "expired": (89.0, {"0xexpired"}),
                "oldest-live": (99.0, {"0xoldest"}),
            }
        )

        privy._cache_user_wallets("new", {"0xnew"}, now=100.0)

        assert set(privy._user_cache) == {"oldest-live", "new"}

        privy._cache_user_wallets("newest", {"0xnewest"}, now=101.0)

        assert set(privy._user_cache) == {"new", "newest"}
        assert len(privy._user_cache) == 2
    finally:
        privy._user_cache.clear()

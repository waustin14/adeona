from .base import Provider, ProviderError
from .mock import MockProvider

__all__ = ["Provider", "ProviderError", "MockProvider", "get_provider"]


def get_provider(name: str, **kwargs) -> Provider:
    if name == "mock":
        return MockProvider(**kwargs)
    if name == "serpapi":
        from .serpapi import SerpApiProvider

        return SerpApiProvider(**kwargs)
    raise ValueError(f"unknown provider '{name}' (choose: mock, serpapi)")

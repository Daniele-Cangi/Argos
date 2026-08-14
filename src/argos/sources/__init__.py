"""Source-specific public REST and WebSocket clients.

Only public market metadata and market-data endpoints. No authenticated,
wallet, or trading surface exists here (ADR-0007).
"""

from argos.sources.clob import (
    ClobBookBadRequestError,
    ClobBookNotFoundError,
    ClobBookResponse,
    ClobClient,
    ClobHealth,
)
from argos.sources.clob_ws import (
    ClobMarketWsClient,
    ClobWsHealth,
    MarketFrame,
    MarketWebSocket,
    WebSocketConnector,
    WebsocketsConnector,
)
from argos.sources.gamma import GammaClient, GammaResponse, SourceHealth

__all__ = [
    "ClobBookBadRequestError",
    "ClobBookNotFoundError",
    "ClobBookResponse",
    "ClobClient",
    "ClobHealth",
    "ClobMarketWsClient",
    "ClobWsHealth",
    "GammaClient",
    "GammaResponse",
    "MarketFrame",
    "MarketWebSocket",
    "SourceHealth",
    "WebSocketConnector",
    "WebsocketsConnector",
]

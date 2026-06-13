"""memnos SDK — backend memory for agentic apps (LangChain / LangGraph / any).

    from memnos_sdk import MemnosClient, AsyncMemnosClient

Framework adapters are optional extras (import only if the framework is installed):
    from memnos_sdk.integrations.langchain import MemnosRetriever   # pip install 'memnos-sdk[langchain]'
    from memnos_sdk.integrations.langgraph import MemnosStore        # pip install 'memnos-sdk[langgraph]'
"""
from .client import AsyncMemnosClient, MemnosClient, MemnosError

try:                                            # single source of truth: the installed package
    from importlib.metadata import PackageNotFoundError, version
    __version__ = version("memnos-sdk")
except Exception:                               # not installed (e.g. running from source)
    __version__ = "0.0.0+unknown"
__all__ = ["MemnosClient", "AsyncMemnosClient", "MemnosError", "__version__"]

"""Read-only Microsoft Graph authentication and Outlook synchronization."""

from inbox_agent.graph.auth import (
    GraphAccessToken,
    GraphAuthenticationError,
    GraphDeviceFlowError,
    GraphLoginRequiredError,
    GraphTokenAcquisitionError,
    GraphTokenCacheError,
    GraphTokenProvider,
)
from inbox_agent.graph.client import (
    GraphAuthorizationError,
    GraphDeltaPage,
    GraphMailClient,
    GraphRequestError,
    GraphServiceError,
    GraphThrottledError,
    GraphURLRejectedError,
)
from inbox_agent.graph.config import (
    GraphAccountAudience,
    GraphSettings,
    GraphSettingsError,
    GraphSettingsNotFoundError,
    GraphSettingsReadError,
    GraphSettingsValidationError,
    GraphSettingsYAMLError,
    load_graph_settings,
)
from inbox_agent.graph.mapper import map_graph_message
from inbox_agent.graph.sync import (
    GraphInboxSynchronizer,
    GraphSyncFailure,
    GraphSyncReport,
    GraphSyncState,
    GraphSyncStorageError,
)

__all__ = [
    "GraphAccessToken",
    "GraphAccountAudience",
    "GraphAuthenticationError",
    "GraphAuthorizationError",
    "GraphDeltaPage",
    "GraphDeviceFlowError",
    "GraphLoginRequiredError",
    "GraphMailClient",
    "GraphRequestError",
    "GraphServiceError",
    "GraphSettings",
    "GraphSettingsError",
    "GraphSettingsNotFoundError",
    "GraphSettingsReadError",
    "GraphSettingsValidationError",
    "GraphSettingsYAMLError",
    "GraphInboxSynchronizer",
    "GraphSyncFailure",
    "GraphSyncReport",
    "GraphSyncState",
    "GraphSyncStorageError",
    "GraphTokenAcquisitionError",
    "GraphTokenCacheError",
    "GraphTokenProvider",
    "GraphThrottledError",
    "GraphURLRejectedError",
    "load_graph_settings",
    "map_graph_message",
]

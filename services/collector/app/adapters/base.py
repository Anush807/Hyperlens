"""The adapter contract every hypervisor integration implements (spec 7.1).

Adapters translate a vendor API into the normalized schema and nothing else:
no database access, no HTTP calls back to the API, no scheduling.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from types import TracebackType

from app.schemas import ClusterIn, DatastoreIn, HostIn, SourceConfig, VMIn

log = logging.getLogger(__name__)


class AdapterError(RuntimeError):
    """Any failure talking to a hypervisor. One source failing is isolated."""


class HypervisorAdapter(ABC):
    def __init__(self, source: SourceConfig) -> None:
        self.source = source

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def list_clusters(self) -> list[ClusterIn]: ...

    @abstractmethod
    def list_hosts(self) -> list[HostIn]: ...

    @abstractmethod
    def list_vms(self) -> list[VMIn]: ...

    @abstractmethod
    def list_datastores(self) -> list[DatastoreIn]: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> HypervisorAdapter:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

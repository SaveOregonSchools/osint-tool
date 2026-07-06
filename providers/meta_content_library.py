from __future__ import annotations

import os
from typing import Any, Iterator


ENV_ENABLED = "META_CONTENT_LIBRARY_ENABLED"


class MetaContentLibraryClient:
    def __init__(self, enabled: bool | None = None):
        raw_enabled = os.getenv(ENV_ENABLED, "false").strip().lower() in {"1", "true", "yes", "on"}
        self.enabled = raw_enabled if enabled is None else enabled
        if not self.enabled:
            raise RuntimeError(
                "Requires approved Meta Content Library API access. Set "
                f"{ENV_ENABLED}=true only after access is approved and the secure API environment is configured."
            )

    def search(self, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        raise RuntimeError(
            "Meta Content Library API access is controlled by Meta/CASD secure environments. "
            "Configure a project-specific adapter before running programmatic searches."
        )

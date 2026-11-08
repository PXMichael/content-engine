"""Content Engine for Hermes Agent.

Turn a long YouTube video into scored clips and per-platform social copy.
Fetch the transcript, score the moments, draft the copy, cut the clips.
"""

from __future__ import annotations

import logging

try:  # package import when Hermes loads the plugin
    from .tools import register_tools, TOOLSET
except ImportError:  # file-path import when tests load the module directly
    from tools import register_tools, TOOLSET

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Plugin entry point, called by the Hermes plugin loader."""
    register_tools(ctx)
    logger.info("content-engine: registered 5 tools in toolset %s", TOOLSET)


__all__ = ["register", "register_tools", "TOOLSET"]

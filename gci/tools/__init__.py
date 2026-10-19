"""The tools. Each module registers its own with @tool on import, and this
imports them all, in the order tools/list shows them, so TOOLS is whole
wherever it is read."""

from . import read, files, run, meta  # noqa: F401
from .registry import TOOLS, describe_tools, tool

__all__ = ["TOOLS", "describe_tools", "tool"]

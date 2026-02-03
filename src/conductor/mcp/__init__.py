"""MCP (Model Context Protocol) integration for Conductor.

This module provides MCP server management for providers that need to
spawn and communicate with MCP servers using the stdio transport.
"""

from conductor.mcp.manager import MCPManager

__all__ = ["MCPManager"]

"""Gates module for Conductor.

This module implements human-in-the-loop gates for interactive
workflow approval and decision points.
"""

from conductor.gates.human import GateResult, HumanGateHandler

__all__ = ["GateResult", "HumanGateHandler"]

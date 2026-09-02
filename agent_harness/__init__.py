"""Agent harness: the agent side of Intentum, kept apart from the backend.

The harness owns what the backend must not: the model loop, the prompts that
frame a task, perception of unstructured sources (documents, video, audio) and
the validation scenarios with their evaluators and measurements. It drives
``agent_backend`` only through the backend's MCP tool surface.

Boundary (MADR 0009, enforced by ``tests/test_boundary.py``):

- modules here import from ``agent_backend`` only its public API (``Backend``,
  ``BackendError``, ``ErrorCode``) and ``agent_backend.mcp.server``;
- nothing under ``agent_backend`` imports this package or names a scenario;
- whatever perception produces enters the backend only through
  ``import_dataset`` or ``attach_metadata``, as a fresh agent output.

The package is not part of the ``agent-backend`` wheel; run its entry points
from the repository root with ``python -m``.
"""

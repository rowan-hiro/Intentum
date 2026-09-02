"""Perception: readers of unstructured sources, on the agent's side of the boundary.

Task workspaces carry more than tables: documents (PDF, markdown), video and
audio. Reading them is probabilistic work and belongs here, not in the
backend, whose contract is structured data with lineage (MADR 0009). A reader
in this package is exposed to the model as a tool beside the backend's own;
what it extracts reaches the backend only through ``import_dataset`` (a table
the agent chose to make) or ``attach_metadata`` (a document the agent chose
to trust), so the backend records it as a fresh agent output with provenance.

No reader exists yet. The first one is chosen by the next measured scenario
task that needs it.
"""

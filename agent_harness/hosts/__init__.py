"""Agent hosts: the programs that run the model loop.

The harness does not own the loop any more (MADR 0011). A host adapter
launches an external agent against the backend's MCP server, with only the
backend's tools enabled, and turns whatever the host records into the
harness's tool-event record, so the convergence metrics and the scenario
scoring apply unchanged. Pacing and perception are the host's; the harness
keeps the scenario, the measurement and these adapters.

``loop`` (agent_harness.loop) is the in-process reference loop kept as the
control arm; ``opencode`` is the first external host.
"""

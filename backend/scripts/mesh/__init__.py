"""Mesh orchestrator — successor to Plum-Snapcast's federation/ package.

Same concepts (peer discovery, state aggregation, endpoint routing); new mechanism:
Sendspin servers dial/reclaim players by URL instead of snapclients roaming to a master.
Exposes a REST surface with parity to the old federation API so the GUI ports with minimal change.
"""

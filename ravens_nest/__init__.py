"""The Raven's Nest — local-first inventory manager.

The append-only event log under data/events/ is the source of truth.
data/cache.db is a disposable SQLite cache rebuilt by replaying events.
"""

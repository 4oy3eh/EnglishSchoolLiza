"""Persistence layer (Phase 2): ORM models + thin repositories.

ORM models in `models` mirror the `contracts/` schemas one-to-one; repositories
in `repository` translate between Pydantic contracts and rows so the rest of the
app only ever speaks contracts.
"""

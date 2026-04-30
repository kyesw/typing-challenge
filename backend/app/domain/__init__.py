"""Pure domain logic for the Typing Game.

Modules under :mod:`app.domain` contain only deterministic, side-effect
free functions and data types. They must not import from FastAPI,
SQLAlchemy, or any other I/O layer so they can be exercised directly by
unit tests, Hypothesis property tests, and the service layer alike.
"""

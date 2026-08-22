class DomainError(Exception):
    """Base error for rejected domain actions."""


class DomainValidationError(DomainError, ValueError):
    """The requested state transition violates a domain invariant."""


class EntityNotFoundError(DomainValidationError):
    """The referenced domain entity does not exist or is no longer active."""

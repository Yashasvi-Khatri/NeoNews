class DatabaseOperationError(RuntimeError):
    """Raised when a database operation fails after rollback/cleanup."""


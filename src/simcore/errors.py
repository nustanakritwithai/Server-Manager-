class GameError(Exception):
    """A command or admin request the server will not apply.

    These are safe to show to the client. Unexpected exceptions are not wrapped
    in GameError and roll the database transaction back.
    """

    def __init__(self, message: str, *, status_code: int = 400, code: str = "invalid_command") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code

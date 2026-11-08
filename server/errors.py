class UserError(Exception):
    def __init__(self, msg, field=None, status=400):
        super().__init__(msg)
        self.field = field
        self.status = status


class WaitingError(UserError):
    """A temporary execution prerequisite; retry when node state changes."""

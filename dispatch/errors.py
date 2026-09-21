"""The one exception type the CLI turns into a message instead of a traceback."""


class DispatchError(Exception):
    """Operator-facing error: printed as `dispatch: <msg>`, exit 2.

    Everything a user can cause (a bad lane, a missing brief, a cap refusal, an
    unreachable substrate) raises this. Anything else is a bug and keeps its
    traceback.
    """

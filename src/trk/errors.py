"""Exit codes and the one error type every layer raises."""

OK = 0
REFUSED = 1
USAGE = 2
NOT_FOUND = 3
CONFLICT = 4

CODE_NAMES = {REFUSED: "refused", USAGE: "usage", NOT_FOUND: "not-found", CONFLICT: "conflict"}


class TrkError(Exception):
    """A refusal or failure the CLI turns into an exit code and one error message."""

    def __init__(self, exit_code, message, rule=None, item=None, extra=None):
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message
        self.rule = rule
        self.item = item
        self.extra = extra or {}

    def to_json(self):
        return {
            "error": {
                "code": CODE_NAMES[self.exit_code],
                "rule": self.rule,
                "item": self.item,
                "message": self.message,
                **self.extra,
            }
        }


def refused(rule, message, item=None):
    return TrkError(REFUSED, message, rule=rule, item=item)


def gate_unmet(item, gate, missing, hint=None):
    """An unmet gate (rule `gate-unmet`): the error JSON carries the gate, its missing list and, when
    given, a `hint` saying how to supply what is missing."""
    extra = {"gate": gate, "missing": missing}
    if hint:
        extra["hint"] = hint
    return TrkError(REFUSED, f"{gate}: {len(missing)} missing", rule="gate-unmet", item=item, extra=extra)


def usage(message, rule=None):
    return TrkError(USAGE, message, rule=rule)


def not_found(message, item=None, rule="not-found"):
    return TrkError(NOT_FOUND, message, rule=rule, item=item)


def conflict(rule, message, item=None):
    return TrkError(CONFLICT, message, rule=rule, item=item)

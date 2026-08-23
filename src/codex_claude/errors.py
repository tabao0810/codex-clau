"""Typed failures exposed by the controller and CLI."""


class OrchestratorError(Exception):
    """Base class for expected user-facing errors."""


class ValidationError(OrchestratorError):
    """Input or agent output violates a contract."""


class StateError(OrchestratorError):
    """Durable state is invalid or incompatible."""


class ConcurrentUpdateError(StateError):
    """State generation changed during a compare-and-swap update."""


class LockError(StateError):
    """A repository or state lock cannot be acquired."""


class GitError(OrchestratorError):
    """A Git command or repository invariant failed."""


class PolicyError(OrchestratorError):
    """An operation is forbidden by the controller security policy."""


class ProcessError(OrchestratorError):
    """A child process failed."""


class ProcessTimeoutError(ProcessError):
    """A child process exceeded its timeout."""


class ContractError(ProcessError):
    """An agent emitted malformed JSON or a schema-invalid result."""


class IntegrationError(OrchestratorError):
    """A patch cannot be validated or integrated safely."""

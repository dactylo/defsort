"""Identify a tool and derive its hook-facing names."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolIdentity:
    """Identity used to name a tool's hook adapter and diagnostic messages.

    Attributes:
        name: Command and distribution name.
        verb: Engine action named in unexpected-error diagnostics.
    """

    name: str
    verb: str

    @property
    def entry_point_group(self) -> str:
        """Return the entry-point group for host adapters."""
        return f'{self.name}.hook_adapters'

    @property
    def reason_prefix(self) -> str:
        """Return the prefix for an unsupported commit-command reason."""
        return f'{self.name}: unsupported commit command'

    @property
    def possible_reason_prefix(self) -> str:
        """Return the prefix for a possible commit-text reason."""
        return f'{self.name}: possible commit text'

    @property
    def deny_fallback(self) -> str:
        """Return the denial message used when the host provides no reason."""
        return f'{self.name} denied the operation'

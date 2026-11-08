"""Re-export the shared claude adapter for defsort."""

from defsort._vendor.hookfront._claude import ClaudeAdapter

__all__ = ['ClaudeAdapter']

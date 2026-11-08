"""Offline-only secure receiver core. Importing never opens a socket or key.

Runtime deployment, service installation and PX4 execution are intentionally
absent from this R1/R2 package. Authentication boundaries require R3 validation.
"""
from .spec import LinkSpec, SecureSessionSpec, SecurityError

__all__ = ['LinkSpec', 'SecureSessionSpec', 'SecurityError']

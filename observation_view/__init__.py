"""Private presentation of one explicitly requested committed observation batch."""
from .errors import ViewError
from .view import create_view

__all__ = ['create_view', 'ViewError']

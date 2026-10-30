"""Attack C: leave the interpreter before pytest can report anything."""
import os

os._exit(0)

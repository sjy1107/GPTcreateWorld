"""Bounded handling for transient Windows sharing conflicts on local records."""
import os
import time


def replace_record(source, target):
    for delay in (0.01, 0.03, 0.06, None):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or delay is None:
                raise
            time.sleep(delay)

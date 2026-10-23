"""Per-item stop requests remain separate from cancelling the entire queue."""
import threading


class ItemControl:
    def __init__(self, cancel):
        self.cancel = cancel
        self.skip = threading.Event()

    def is_set(self):
        return self.cancel.is_set() or self.skip.is_set()

    def wait(self, timeout):
        return self.skip.wait(timeout) or self.cancel.is_set()

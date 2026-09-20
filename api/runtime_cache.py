"""Small thread-safe caches for reconstructible dashboard data only."""
from collections import OrderedDict
from collections.abc import MutableMapping
from datetime import datetime
from threading import RLock


class BoundedTimestampCache(MutableMapping):
    """Values start with a UTC fetch timestamp. Expire on reads and writes.

    A separate retention window can exceed freshness TTL for vendor-outage
    fallback. Eviction never affects authoritative database records.
    """
    def __init__(self, *, max_entries, retention):
        self.max_entries = max_entries
        self.retention = retention
        self._data = OrderedDict()
        self._lock = RLock()

    def _expire(self):
        cutoff = datetime.utcnow() - self.retention
        for key in [k for k, v in self._data.items() if v[0] <= cutoff]:
            del self._data[key]

    def __getitem__(self, key):
        with self._lock:
            self._expire()
            value = self._data[key]
            self._data.move_to_end(key)
            return value

    def __setitem__(self, key, value):
        with self._lock:
            self._expire()
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def __delitem__(self, key):
        with self._lock:
            del self._data[key]

    def __iter__(self):
        with self._lock:
            self._expire()
            return iter(list(self._data))

    def __len__(self):
        with self._lock:
            self._expire()
            return len(self._data)

    def clear(self):
        with self._lock:
            self._data.clear()

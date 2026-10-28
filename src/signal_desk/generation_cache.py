"""Cache no-argument computations against their input generation."""

from functools import lru_cache, wraps
from threading import RLock


def generation_cache(generation):
    def decorate(fn):
        lock = RLock()
        epoch = 0

        @lru_cache(maxsize=1)
        def cached(key):
            return fn()

        def key():
            with lock:
                return epoch, generation()

        @wraps(fn)
        def read():
            # A clear during calculation must not publish a retired result as
            # current. Retry once; continuous updates retain the actual input
            # metadata and never become a hit for the newer generation.
            for _ in range(2):
                before = key()
                value = cached(before)
                if key() == before:
                    break
            return value

        def clear():
            nonlocal epoch
            with lock:
                epoch += 1
                cached.cache_clear()

        read.cache_clear = clear
        read.cache_info = cached.cache_info
        return read
    return decorate

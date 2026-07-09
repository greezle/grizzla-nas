"""In-process event bus: workers/services publish "something changed"
nudges, SSE connections subscribe. Deliberately tiny - messages carry a kind
and optionally a hostname, never data; clients re-fetch what they need, so
rendering stays in one place and a lost nudge costs nothing (the pages keep
a slow fallback refresh).
"""
import queue
import threading

_lock = threading.Lock()
_subscribers = []


def subscribe(maxsize=64):
    q = queue.Queue(maxsize=maxsize)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q):
    with _lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def publish(kind, host=None):
    """Non-blocking fan-out. A slow/full subscriber just misses this nudge -
    its browser still has the fallback poll, so nothing is ever stuck."""
    msg = {"kind": kind}
    if host:
        msg["host"] = host
    with _lock:
        subscribers = list(_subscribers)
    for q in subscribers:
        try:
            q.put_nowait(msg)
        except queue.Full:
            pass

import os
import threading

workers    = 1          # Sirf 1 worker — trading bot ke liye enough
threads    = 4          # Per worker threads
timeout    = 120
bind       = f"0.0.0.0:{os.environ.get('PORT', 10000)}"
worker_class = "gthread"
keepalive  = 5
loglevel   = "info"

# Health check ke liye port immediately open hona chahiye
graceful_timeout = 30
preload_app      = False   # Fork ke BAAD import karo — threads safe rahenge

def post_fork(server, worker):
    """Worker fork ke baad — yahan threads start karo"""
    import time
    def _delayed_startup():
        time.sleep(2)  # Worker settle hone do
        try:
            from main import _startup_once
            _startup_once()
        except Exception as e:
            print(f"post_fork startup error: {e}")
    threading.Thread(target=_delayed_startup, daemon=True).start()
    print(f"Worker {worker.pid} forked — startup scheduled")

import os

bind             = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers          = 1
worker_class     = "sync"
timeout          = 300
keepalive        = 2
preload_app      = False  # FIX: True se daemon threads fork pe mar jaate hain — positions restore nahi hoti
loglevel         = "info"
graceful_timeout = 30

def post_fork(server, worker):
    """Worker start hone ke baad startup karo — threads yahan survive karte hain"""
    import threading
    try:
        from main import _startup_once
        threading.Thread(target=_startup_once, daemon=True).start()
    except Exception as e:
        print(f"post_fork error: {e}")

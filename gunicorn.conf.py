import os

bind             = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers          = 1
worker_class     = "gthread"
threads          = 4
timeout          = 120
keepalive        = 5
preload_app      = False
loglevel         = "info"
graceful_timeout = 30
worker_exit_on_app_init_error = False

def post_fork(server, worker):
    import threading
    try:
        from main import _startup_once
        threading.Thread(target=_startup_once, daemon=True).start()
    except Exception as e:
        print(f"post_fork error: {e}")

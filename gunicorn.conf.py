import os

bind             = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers          = 1
threads          = 6
worker_class     = "gthread"
timeout          = 120        # was 300 — 2 min enough, prevents zombie workers
keepalive        = 5
preload_app      = False      # False rakho — startup blocking avoid hoga
loglevel         = "info"
graceful_timeout = 30         # was 60 — faster restart on redeploy
worker_exit_on_fail = True

# Render health check ke liye — worker turant serve kare
def post_fork(server, worker):
    pass  # startup_once() ab before_request se hota hai, yahan kuch nahi

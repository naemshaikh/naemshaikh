import os

bind             = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers          = 1
threads          = 2
worker_class     = "gthread"
timeout          = 120
keepalive        = 5
preload_app      = False
loglevel         = "info"
graceful_timeout = 30

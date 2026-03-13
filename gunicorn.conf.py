import os

bind             = "0.0.0.0:" + os.environ.get("PORT", "10000")
workers          = 1
worker_class     = "sync"      # sync = lightest, most reliable on Render free tier
timeout          = 300          # 5 min — web3+supabase import time ke liye
keepalive        = 2
preload_app      = True         # port bind pehle, then load app
loglevel         = "info"
graceful_timeout = 30

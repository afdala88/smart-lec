# gunicorn -c deploy/gunicorn.conf.py app:app
#
# ONE worker process on purpose: the census queue worker thread and the
# cancel/stop events live inside that process (core/census_queue.py).
# Concurrency for many users comes from threads, not processes.
bind             = '127.0.0.1:5001'      # nginx in front terminates HTTPS
workers          = 1
threads          = 8
worker_class     = 'gthread'
timeout          = 300                    # large photo uploads on slow links
graceful_timeout = 30
accesslog        = '-'
errorlog         = '-'

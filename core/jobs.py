import threading

# Census job registry  { job_id: { 'status': 'running'|'done'|'error', 'result': {...}, 'error': str } }
_jobs: dict = {}
_jobs_lock = threading.Lock()

# Develop/training job registry  { job_id: { 'status', 'log', 'error' } }
_dev_jobs: dict = {}

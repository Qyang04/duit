"""Background jobs (Procrastinate): work that should not run inside an HTTP request.

The queue lives in Postgres (tables procrastinate_*), so there is no Redis to run, back up or
secure, and the CNPG backups already cover queued jobs. Run a worker with:

    procrastinate --app=duit.jobs.app.app worker
"""

import os

# Settings are required-by-default, so the environment has to be complete before
# `app.config` is imported anywhere. CI sets these itself.
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("ENVIRONMENT", "test")

import time
from functools import wraps
from django.db import transaction

# PostgreSQL 예: 직렬화 실패/데드락 에러코드
PG_RETRY_ERRCODES = {'40001', '40P01'}

def _pgcode_from(exc: Exception):
    return getattr(exc, 'pgcode', None) or getattr(getattr(exc, '__cause__', None), 'pgcode', None)

def is_retryable(exc: Exception) -> bool:
    code = _pgcode_from(exc)
    if code and code in PG_RETRY_ERRCODES:
        return True
    msg = str(exc).lower()
    return any(k in msg for k in ('deadlock detected', 'could not serialize access'))

def retry_on_tx_failure(max_attempts=3, backoff=0.05):
    """idempotent 구간에만 적용할 것!"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                attempt += 1
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    if attempt >= max_attempts or not is_retryable(e):
                        raise
                    time.sleep(backoff * attempt)
        return wrapper
    return deco

@retry_on_tx_failure(max_attempts=5, backoff=0.1)
@transaction.atomic
def safe_increment_counter(model_cls, pk):
    obj = model_cls.objects.select_for_update().get(pk=pk)
    obj.counter = obj.counter + 1
    obj.save(update_fields=['counter'])
    return obj.counter

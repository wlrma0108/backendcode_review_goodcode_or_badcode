from django.db import models

class IdempotencyKey(models.Model):
    key = models.CharField(max_length=128, unique=True)
    user = models.ForeignKey("users.User", on_delete=models.CASCADE)
    request_hash = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

from django.db import models

class ZohoToken(models.Model):
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_issued_time = models.DateTimeField()

    def __str__(self):
        return f"Zoho Token (Issued: {self.token_issued_time})"

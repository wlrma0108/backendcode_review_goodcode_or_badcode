from django.db import models

class Product(models.Model):  
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def apply_discount(self, percent):  
        self.price *= (1 - percent/100)
        return self.price

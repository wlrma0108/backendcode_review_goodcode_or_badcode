from django.views import View
from django.http import HttpResponse

class HelloView(View):
    def get(self, request):
        return HttpResponse("Hello, World!")

class GoodbyeView(HelloView):  
    def get(self, request): 
        return HttpResponse("Goodbye!")

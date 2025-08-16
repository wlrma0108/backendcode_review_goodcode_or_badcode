class SimpleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response 

    def __call__(self, request):  
        print(f"[요청 시작] {request.path}")
        response = self.get_response(request)
        print(f"[응답 완료] {response.status_code}")
        return response

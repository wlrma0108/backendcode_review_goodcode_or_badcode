# consumers.py
from channels.generic.websocket import WebsocketConsumer
import json

class AuctionConsumer(WebsocketConsumer):

    
    def connect(self):
        # 연결 수락
        self.accept()
        
    def disconnect(self, close_code):
        # 연결 종료
        pass
    
    def receive(self, text_data):
        # 클라이언트로부터 메시지 받음
        data = json.loads(text_data)
        
        # 입찰 처리 (매우 단순)
        if data['type'] == 'bid':
            auction_id = data['auction_id']
            bid_amount = data['amount']
            
            # 여기서 입찰 처리 로직 (DB 저장 등)
            # 문제: 동기 방식이라 DB 작업 중 다른 요청 블로킹됨
            
            # 응답 전송
            self.send(text_data=json.dumps({
                'type': 'bid_update',
                'auction_id': auction_id,
                'amount': bid_amount
            }))


# routing.py
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/auction/$', consumers.AuctionConsumer.as_asgi()),
]


# settings.py
INSTALLED_APPS = [
    'daphne',  # ASGI 서버
    'channels',
    # ... 기타 앱들
]

ASGI_APPLICATION = 'myproject.asgi.application'


# asgi.py
import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from myapp import routing

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myproject.settings')

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(
            routing.websocket_urlpatterns
        )
    ),
})
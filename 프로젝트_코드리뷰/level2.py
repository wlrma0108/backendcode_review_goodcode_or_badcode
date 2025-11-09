
# consumers.py
from channels.generic.websocket import AsyncWebsocketConsumer
import json
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser

class AuctionConsumer(AsyncWebsocketConsumer):
    """
    비동기 WebSocket with 채널 그룹
    
    개선점:
    1. async/await로 비블로킹 처리
    2. 채널 그룹으로 같은 경매 참여자들에게 브로드캐스트
    3. 인증 처리 추가
    
    여전한 문제:
    1. 메모리에만 그룹 정보 저장 (서버 재시작 시 소실)
    2. 멀티 서버 환경에서 동작 안 함
    3. 재연결 시 이전 상태 복구 불가
    """
    
    async def connect(self):
        # URL에서 경매 ID 추출
        self.auction_id = self.scope['url_route']['kwargs']['auction_id']
        self.auction_group_name = f'auction_{self.auction_id}'
        
        # 인증 확인
        self.user = self.scope["user"]
        if self.user == AnonymousUser():
            await self.close()
            return
        
        # 채널 그룹에 추가
        await self.channel_layer.group_add(
            self.auction_group_name,
            self.channel_name
        )
        
        await self.accept()
        
        # 연결 성공 메시지
        await self.send(text_data=json.dumps({
            'type': 'connection_established',
            'message': f'Connected to auction {self.auction_id}'
        }))
        
        print(f"User {self.user.id} connected to auction {self.auction_id}")
    
    async def disconnect(self, close_code):
        # 채널 그룹에서 제거
        await self.channel_layer.group_discard(
            self.auction_group_name,
            self.channel_name
        )
        
        print(f"User {self.user.id} disconnected from auction {self.auction_id}")
    
    async def receive(self, text_data):
        """클라이언트로부터 메시지 수신"""
        try:
            data = json.loads(text_data)
            
            if data['type'] == 'bid':
                await self.handle_bid(data)
            elif data['type'] == 'ping':
                # Keep-alive
                await self.send(text_data=json.dumps({'type': 'pong'}))
                
        except json.JSONDecodeError:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': 'Invalid JSON'
            }))
        except Exception as e:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': str(e)
            }))
    
    async def handle_bid(self, data):
        """입찰 처리"""
        bid_amount = data['amount']
        
        # DB 작업은 동기이므로 database_sync_to_async로 감싸기
        result = await self.save_bid(self.auction_id, self.user.id, bid_amount)
        
        if result['success']:
            # 같은 그룹의 모든 사용자에게 브로드캐스트
            await self.channel_layer.group_send(
                self.auction_group_name,
                {
                    'type': 'bid_update',  # 이 이름은 아래 메서드명과 매칭
                    'auction_id': self.auction_id,
                    'user_id': self.user.id,
                    'username': self.user.username,
                    'amount': bid_amount,
                    'timestamp': result['timestamp']
                }
            )
        else:
            # 입찰 실패 시 해당 사용자에게만 에러 전송
            await self.send(text_data=json.dumps({
                'type': 'bid_error',
                'message': result['error']
            }))
    
    async def bid_update(self, event):
        """
        채널 그룹으로부터 받은 메시지를 클라이언트로 전송
        메서드명은 group_send의 'type'과 일치해야 함 (점을 언더스코어로)
        """
        await self.send(text_data=json.dumps({
            'type': 'bid_update',
            'auction_id': event['auction_id'],
            'user_id': event['user_id'],
            'username': event['username'],
            'amount': event['amount'],
            'timestamp': event['timestamp']
        }))
    
    @database_sync_to_async
    def save_bid(self, auction_id, user_id, amount):
        """동기 DB 작업을 비동기로 감싸기"""
        from .models import Auction, Bid
        from django.utils import timezone
        
        try:
            auction = Auction.objects.select_for_update().get(id=auction_id)
            
            # 입찰 검증
            if amount <= auction.current_price:
                return {
                    'success': False,
                    'error': f'Bid must be higher than {auction.current_price}'
                }
            
            if auction.is_ended:
                return {
                    'success': False,
                    'error': 'Auction has ended'
                }
            
            # 입찰 저장
            bid = Bid.objects.create(
                auction=auction,
                user_id=user_id,
                amount=amount,
                timestamp=timezone.now()
            )
            
            # 현재가 업데이트
            auction.current_price = amount
            auction.current_winner_id = user_id
            auction.save()
            
            return {
                'success': True,
                'timestamp': bid.timestamp.isoformat()
            }
            
        except Auction.DoesNotExist:
            return {
                'success': False,
                'error': 'Auction not found'
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }


# routing.py
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/auction/(?P<auction_id>\w+)/$', consumers.AuctionConsumer.as_asgi()),
]


# settings.py
INSTALLED_APPS = [
    'daphne',
    'channels',
    # ...
]

ASGI_APPLICATION = 'myproject.asgi.application'

# 채널 레이어 설정 - 아직 메모리만 사용 (단일 서버)
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer'
    }
}
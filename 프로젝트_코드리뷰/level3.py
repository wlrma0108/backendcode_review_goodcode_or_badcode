
# settings.py
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            "hosts": [('redis', 6379)],
            # 연결 풀 설정
            "capacity": 1500,  # 채널당 최대 메시지 수
            "expiry": 10,  # 메시지 만료 시간 (초)
        },
    },
}


# consumers.py
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import json
import asyncio
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class AuctionConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.auction_id = self.scope['url_route']['kwargs']['auction_id']
        self.auction_group_name = f'auction_{self.auction_id}'
        self.user = self.scope["user"]
        
        # 인증 체크
        if not self.user.is_authenticated:
            await self.close(code=4001)
            return
        
        # 경매 존재 여부 확인
        auction_exists = await self.check_auction_exists(self.auction_id)
        if not auction_exists:
            await self.close(code=4004)
            return
        
        # 그룹 참가
        await self.channel_layer.group_add(
            self.auction_group_name,
            self.channel_name
        )
        
        await self.accept()
        
        # 현재 경매 상태 전송
        current_state = await self.get_auction_state(self.auction_id)
        await self.send(text_data=json.dumps({
            'type': 'initial_state',
            'data': current_state
        }))
        
        logger.info(f"User {self.user.id} connected to auction {self.auction_id}")
        
        # Keep-alive 태스크 시작
        self.keep_alive_task = asyncio.create_task(self.keep_alive())
    
    async def disconnect(self, close_code):
        # Keep-alive 태스크 취소
        if hasattr(self, 'keep_alive_task'):
            self.keep_alive_task.cancel()
        
        # 그룹에서 제거
        await self.channel_layer.group_discard(
            self.auction_group_name,
            self.channel_name
        )
        
        logger.info(
            f"User {self.user.id} disconnected from auction {self.auction_id} "
            f"with code {close_code}"
        )
    
    async def keep_alive(self):
        """
        주기적으로 ping을 보내 연결 유지
        클라이언트가 응답하지 않으면 연결 종료
        """
        try:
            while True:
                await asyncio.sleep(30)  # 30초마다
                await self.send(text_data=json.dumps({
                    'type': 'ping',
                    'timestamp': datetime.now().isoformat()
                }))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Keep-alive error: {e}")
    
    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            if message_type == 'bid':
                await self.handle_bid(data)
            elif message_type == 'pong':
                # 클라이언트 응답 받음
                pass
            else:
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': f'Unknown message type: {message_type}'
                }))
                
        except json.JSONDecodeError:
            await self.send_error('Invalid JSON format')
        except KeyError as e:
            await self.send_error(f'Missing required field: {e}')
        except Exception as e:
            logger.error(f"Receive error: {e}", exc_info=True)
            await self.send_error('Internal server error')
    
    async def handle_bid(self, data):
        """입찰 처리"""
        try:
            bid_amount = int(data['amount'])
            
            if bid_amount <= 0:
                await self.send_error('Bid amount must be positive')
                return
            
            # 입찰 저장 및 검증
            result = await self.process_bid(
                self.auction_id,
                self.user.id,
                bid_amount
            )
            
            if result['success']:
                # 모든 서버의 모든 클라이언트에게 브로드캐스트
                # Redis pub/sub을 통해 전달됨
                await self.channel_layer.group_send(
                    self.auction_group_name,
                    {
                        'type': 'bid_update',
                        'auction_id': self.auction_id,
                        'user_id': self.user.id,
                        'username': self.user.username,
                        'amount': bid_amount,
                        'timestamp': result['timestamp'],
                        'bid_count': result['bid_count']
                    }
                )
                
                logger.info(
                    f"Bid placed: auction={self.auction_id}, "
                    f"user={self.user.id}, amount={bid_amount}"
                )
            else:
                await self.send_error(result['error'])
                
        except ValueError:
            await self.send_error('Invalid bid amount')
        except Exception as e:
            logger.error(f"Bid handling error: {e}", exc_info=True)
            await self.send_error('Failed to process bid')
    
    async def bid_update(self, event):
        """그룹으로부터 받은 입찰 업데이트를 클라이언트로 전송"""
        await self.send(text_data=json.dumps({
            'type': 'bid_update',
            'auction_id': event['auction_id'],
            'user_id': event['user_id'],
            'username': event['username'],
            'amount': event['amount'],
            'timestamp': event['timestamp'],
            'bid_count': event['bid_count']
        }))
    
    async def send_error(self, message):
        """에러 메시지 전송"""
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message': message,
            'timestamp': datetime.now().isoformat()
        }))
    
    @database_sync_to_async
    def check_auction_exists(self, auction_id):
        """경매 존재 여부 확인"""
        from .models import Auction
        return Auction.objects.filter(id=auction_id).exists()
    
    @database_sync_to_async
    def get_auction_state(self, auction_id):
        """현재 경매 상태 조회"""
        from .models import Auction
        
        try:
            auction = Auction.objects.get(id=auction_id)
            return {
                'current_price': auction.current_price,
                'current_winner_id': auction.current_winner_id,
                'bid_count': auction.bid_count,
                'end_time': auction.end_time.isoformat(),
                'status': auction.status
            }
        except Auction.DoesNotExist:
            return None
    
    @database_sync_to_async
    def process_bid(self, auction_id, user_id, amount):
        """입찰 처리 및 저장"""
        from .models import Auction, Bid
        from django.utils import timezone
        from django.db import transaction
        
        try:
            with transaction.atomic():
                # select_for_update로 row lock
                auction = Auction.objects.select_for_update().get(id=auction_id)
                
                # 입찰 검증
                if auction.status != 'active':
                    return {'success': False, 'error': 'Auction is not active'}
                
                if amount <= auction.current_price:
                    return {
                        'success': False,
                        'error': f'Bid must be higher than {auction.current_price}'
                    }
                
                if timezone.now() > auction.end_time:
                    auction.status = 'ended'
                    auction.save()
                    return {'success': False, 'error': 'Auction has ended'}
                
                # 재화 차감 로직 (여기서는 간단히 표현)
                # 실제로는 재화 잠금 처리 필요
                
                # 입찰 생성
                bid = Bid.objects.create(
                    auction=auction,
                    user_id=user_id,
                    amount=amount,
                    timestamp=timezone.now()
                )
                
                # 경매 상태 업데이트
                auction.current_price = amount
                auction.current_winner_id = user_id
                auction.bid_count += 1
                auction.save()
                
                return {
                    'success': True,
                    'timestamp': bid.timestamp.isoformat(),
                    'bid_count': auction.bid_count
                }
                
        except Auction.DoesNotExist:
            return {'success': False, 'error': 'Auction not found'}
        except Exception as e:
            logger.error(f"Process bid error: {e}", exc_info=True)
            return {'success': False, 'error': 'Internal error'}
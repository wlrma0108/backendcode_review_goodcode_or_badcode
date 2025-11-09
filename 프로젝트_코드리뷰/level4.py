from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import json
import asyncio
from datetime import datetime, timedelta
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AuctionConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.auction_id = self.scope['url_route']['kwargs']['auction_id']
        self.auction_group_name = f'auction_{self.auction_id}'
        self.user = self.scope["user"]
        
        # 연결 메타데이터
        self.connection_id = self.scope['path'].split('/')[-1]  # 클라이언트 제공
        self.last_sequence = 0  # 마지막으로 받은 시퀀스 번호
        
        if not self.user.is_authenticated:
            await self.close(code=4001)
            return
        
        auction_exists = await self.check_auction_exists(self.auction_id)
        if not auction_exists:
            await self.close(code=4004)
            return
        
        await self.channel_layer.group_add(
            self.auction_group_name,
            self.channel_name
        )
        
        await self.accept()
        
        # 재연결인지 확인
        query_string = self.scope['query_string'].decode()
        params = dict(param.split('=') for param in query_string.split('&') if '=' in param)
        last_seq = int(params.get('last_seq', 0))
        
        if last_seq > 0:
            # 재연결: 누락된 메시지 전송
            await self.handle_reconnect(last_seq)
        else:
            # 첫 연결: 초기 상태 전송
            await self.send_initial_state()
        
        logger.info(
            f"User {self.user.id} connected to auction {self.auction_id}, "
            f"last_seq={last_seq}"
        )
        
        self.keep_alive_task = asyncio.create_task(self.keep_alive())
    
    async def disconnect(self, close_code):
        if hasattr(self, 'keep_alive_task'):
            self.keep_alive_task.cancel()
        
        await self.channel_layer.group_discard(
            self.auction_group_name,
            self.channel_name
        )
        
        logger.info(
            f"User {self.user.id} disconnected from auction {self.auction_id}, "
            f"last_seq={self.last_sequence}, code={close_code}"
        )
    
    async def handle_reconnect(self, last_seq: int):
        """
        재연결 처리: 누락된 메시지 전송
        
        Redis에 저장된 메시지 히스토리에서
        last_seq 이후의 메시지들을 가져와 전송
        """
        try:
            # Redis에서 메시지 히스토리 조회
            missed_messages = await self.get_message_history(
                self.auction_id,
                last_seq
            )
            
            if missed_messages:
                await self.send(text_data=json.dumps({
                    'type': 'reconnect_sync',
                    'missed_count': len(missed_messages),
                    'messages': missed_messages
                }))
                
                logger.info(
                    f"Sent {len(missed_messages)} missed messages to "
                    f"user {self.user.id}"
                )
            else:
                await self.send_initial_state()
                
        except Exception as e:
            logger.error(f"Reconnect handling error: {e}", exc_info=True)
            # 에러 시 초기 상태라도 전송
            await self.send_initial_state()
    
    async def send_initial_state(self):
        """초기 경매 상태 전송"""
        state = await self.get_auction_state(self.auction_id)
        current_seq = await self.get_current_sequence(self.auction_id)
        
        await self.send(text_data=json.dumps({
            'type': 'initial_state',
            'sequence': current_seq,
            'data': state
        }))
        
        self.last_sequence = current_seq
    
    async def keep_alive(self):
        """연결 유지 및 타임아웃 감지"""
        timeout_count = 0
        max_timeout = 3
        
        try:
            while True:
                await asyncio.sleep(30)
                
                # Ping 전송
                await self.send(text_data=json.dumps({
                    'type': 'ping',
                    'timestamp': datetime.now().isoformat(),
                    'sequence': await self.get_current_sequence(self.auction_id)
                }))
                
                # Pong 응답 대기 (클라이언트가 receive에서 pong 전송해야 함)
                # 실제로는 더 복잡한 타임아웃 로직 필요
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Keep-alive error: {e}")
            await self.close()
    
    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            if message_type == 'bid':
                await self.handle_bid(data)
            elif message_type == 'pong':
                # 클라이언트 응답 받음
                client_seq = data.get('sequence', 0)
                if client_seq < self.last_sequence:
                    # 클라이언트가 뒤처짐, 동기화 필요
                    await self.handle_reconnect(client_seq)
            elif message_type == 'ack':
                # 메시지 수신 확인
                ack_seq = data.get('sequence')
                if ack_seq:
                    self.last_sequence = max(self.last_sequence, ack_seq)
            else:
                await self.send_error(f'Unknown message type: {message_type}')
                
        except json.JSONDecodeError:
            await self.send_error('Invalid JSON format')
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
            
            # 입찰 처리
            result = await self.process_bid(
                self.auction_id,
                self.user.id,
                bid_amount
            )
            
            if result['success']:
                # 시퀀스 번호 증가
                sequence = await self.increment_sequence(self.auction_id)
                
                # 메시지 생성
                message = {
                    'type': 'bid_update',
                    'sequence': sequence,
                    'auction_id': self.auction_id,
                    'user_id': self.user.id,
                    'username': self.user.username,
                    'amount': bid_amount,
                    'timestamp': result['timestamp'],
                    'bid_count': result['bid_count']
                }
                
                # Redis에 메시지 히스토리 저장 (TTL 설정)
                await self.save_message_history(
                    self.auction_id,
                    sequence,
                    message
                )
                
                # 그룹에 브로드캐스트
                await self.channel_layer.group_send(
                    self.auction_group_name,
                    {
                        'type': 'broadcast_message',
                        'message': message
                    }
                )
                
                logger.info(
                    f"Bid placed: auction={self.auction_id}, "
                    f"user={self.user.id}, amount={bid_amount}, seq={sequence}"
                )
            else:
                await self.send_error(result['error'])
                
        except ValueError:
            await self.send_error('Invalid bid amount')
        except Exception as e:
            logger.error(f"Bid handling error: {e}", exc_info=True)
            await self.send_error('Failed to process bid')
    
    async def broadcast_message(self, event):
        """그룹으로부터 받은 메시지를 클라이언트로 전송"""
        message = event['message']
        sequence = message['sequence']
        
        # 순서대로 전송 보장
        if sequence <= self.last_sequence:
            # 이미 받은 메시지, 무시
            return
        
        await self.send(text_data=json.dumps(message))
        self.last_sequence = sequence
    
    async def send_error(self, message: str):
        """에러 메시지 전송"""
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message': message,
            'timestamp': datetime.now().isoformat()
        }))
    
    # Redis 작업 헬퍼 메서드들
    
    async def get_current_sequence(self, auction_id: str) -> int:
        """현재 시퀀스 번호 조회"""
        from channels.layers import get_channel_layer
        import redis.asyncio as redis
        
        channel_layer = get_channel_layer()
        # Redis 클라이언트 직접 접근 (channels_redis 내부 구조 활용)
        redis_client = await redis.from_url('redis://redis:6379')
        
        try:
            key = f'auction:{auction_id}:sequence'
            seq = await redis_client.get(key)
            return int(seq) if seq else 0
        finally:
            await redis_client.close()
    
    async def increment_sequence(self, auction_id: str) -> int:
        """시퀀스 번호 증가 (원자적 연산)"""
        import redis.asyncio as redis
        
        redis_client = await redis.from_url('redis://redis:6379')
        
        try:
            key = f'auction:{auction_id}:sequence'
            new_seq = await redis_client.incr(key)
            # TTL 설정 (경매 종료 후 자동 삭제)
            await redis_client.expire(key, 86400)  # 24시간
            return new_seq
        finally:
            await redis_client.close()
    
    async def save_message_history(self, auction_id: str, sequence: int, message: dict):
        """메시지 히스토리 저장"""
        import redis.asyncio as redis
        
        redis_client = await redis.from_url('redis://redis:6379')
        
        try:
            key = f'auction:{auction_id}:history'
            # Sorted Set에 저장 (score는 sequence)
            await redis_client.zadd(
                key,
                {json.dumps(message): sequence}
            )
            # TTL 설정
            await redis_client.expire(key, 3600)  # 1시간
            
            # 오래된 메시지 제거 (최근 1000개만 유지)
            await redis_client.zremrangebyrank(key, 0, -1001)
        finally:
            await redis_client.close()
    
    async def get_message_history(self, auction_id: str, after_seq: int) -> list:
        """특정 시퀀스 이후의 메시지 히스토리 조회"""
        import redis.asyncio as redis
        
        redis_client = await redis.from_url('redis://redis:6379')
        
        try:
            key = f'auction:{auction_id}:history'
            # after_seq보다 큰 메시지들 조회
            messages = await redis_client.zrangebyscore(
                key,
                after_seq + 1,
                '+inf'
            )
            return [json.loads(msg) for msg in messages]
        finally:
            await redis_client.close()
    
    @database_sync_to_async
    def check_auction_exists(self, auction_id):
        from .models import Auction
        return Auction.objects.filter(id=auction_id).exists()
    
    @database_sync_to_async
    def get_auction_state(self, auction_id):
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
        from .models import Auction, Bid
        from django.utils import timezone
        from django.db import transaction
        
        try:
            with transaction.atomic():
                auction = Auction.objects.select_for_update().get(id=auction_id)
                
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
                
                bid = Bid.objects.create(
                    auction=auction,
                    user_id=user_id,
                    amount=amount,
                    timestamp=timezone.now()
                )
                
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
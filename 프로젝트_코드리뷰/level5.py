
# consumers.py
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import json
import asyncio
from datetime import datetime, timedelta
import logging
from typing import Optional, Dict, Any
import time
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class RedisConnectionPool:
    """Redis 연결 풀 관리"""
    _pool = None
    
    @classmethod
    async def get_pool(cls):
        if cls._pool is None:
            import redis.asyncio as redis
            cls._pool = redis.ConnectionPool.from_url(
                'redis://redis:6379',
                max_connections=50,
                decode_responses=True
            )
        return cls._pool
    
    @classmethod
    @asynccontextmanager
    async def get_connection(cls):
        """컨텍스트 매니저로 Redis 연결 관리"""
        import redis.asyncio as redis
        pool = await cls.get_pool()
        client = redis.Redis(connection_pool=pool)
        try:
            yield client
        finally:
            await client.close()


class CircuitBreaker:
    """
    Circuit Breaker 패턴
    연속 실패 시 일시적으로 요청 차단하여 시스템 보호
    """
    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failures = 0
        self.last_failure_time = None
        self.state = 'closed'  # closed, open, half_open
    
    async def call(self, func, *args, **kwargs):
        if self.state == 'open':
            if time.time() - self.last_failure_time > self.timeout:
                self.state = 'half_open'
            else:
                raise Exception("Circuit breaker is open")
        
        try:
            result = await func(*args, **kwargs)
            if self.state == 'half_open':
                self.state = 'closed'
                self.failures = 0
            return result
        except Exception as e:
            self.failures += 1
            self.last_failure_time = time.time()
            
            if self.failures >= self.failure_threshold:
                self.state = 'open'
                logger.error(f"Circuit breaker opened after {self.failures} failures")
            
            raise e


class AuctionConsumer(AsyncWebsocketConsumer):
    """
    프로덕션급 WebSocket Consumer
    
    특징:
    1. Circuit Breaker로 Redis 장애 대응
    2. 연결 풀 재사용
    3. 상세한 메트릭 수집
    4. Graceful degradation
    5. 백프레셔(backpressure) 처리
    6. 메모리 효율적 메시지 버퍼링
    """
    
    # 클래스 레벨 Circuit Breaker
    redis_circuit_breaker = CircuitBreaker()
    
    # 메트릭 수집
    metrics = {
        'connections': 0,
        'messages_sent': 0,
        'messages_received': 0,
        'errors': 0,
        'reconnects': 0
    }
    
    async def connect(self):
        """연결 수립"""
        start_time = time.time()
        
        try:
            # 기본 설정
            self.auction_id = self.scope['url_route']['kwargs']['auction_id']
            self.auction_group_name = f'auction_{self.auction_id}'
            self.user = self.scope["user"]
            
            # 상태 관리
            self.last_sequence = 0
            self.pending_acks = set()  # 확인 대기 중인 시퀀스 번호들
            self.message_buffer = []  # 클라이언트로 전송할 메시지 버퍼
            self.is_healthy = True
            
            # 인증 체크
            if not self.user.is_authenticated:
                await self.close(code=4001)
                return
            
            # 경매 검증
            auction_exists = await self.check_auction_exists(self.auction_id)
            if not auction_exists:
                await self.close(code=4004)
                return
            
            # Rate limiting 체크
            if not await self.check_rate_limit():
                await self.close(code=4029)  # Too Many Requests
                return
            
            # 그룹 참가
            await self.channel_layer.group_add(
                self.auction_group_name,
                self.channel_name
            )
            
            await self.accept()
            
            # 재연결 처리
            query_string = self.scope['query_string'].decode()
            params = self._parse_query_string(query_string)
            last_seq = int(params.get('last_seq', 0))
            
            if last_seq > 0:
                await self.handle_reconnect(last_seq)
                self.metrics['reconnects'] += 1
            else:
                await self.send_initial_state()
            
            # 백그라운드 태스크 시작
            self.keep_alive_task = asyncio.create_task(self.keep_alive())
            self.message_sender_task = asyncio.create_task(self.message_sender())
            self.health_check_task = asyncio.create_task(self.health_check())
            
            # 메트릭 업데이트
            self.metrics['connections'] += 1
            
            connection_time = time.time() - start_time
            logger.info(
                f"Connection established: user={self.user.id}, "
                f"auction={self.auction_id}, last_seq={last_seq}, "
                f"time={connection_time:.3f}s"
            )
            
        except Exception as e:
            logger.error(f"Connection error: {e}", exc_info=True)
            await self.close(code=1011)  # Internal error
    
    async def disconnect(self, close_code):
        """연결 종료"""
        # 백그라운드 태스크 정리
        for task_name in ['keep_alive_task', 'message_sender_task', 'health_check_task']:
            if hasattr(self, task_name):
                task = getattr(self, task_name)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # 그룹에서 제거
        try:
            await self.channel_layer.group_discard(
                self.auction_group_name,
                self.channel_name
            )
        except Exception as e:
            logger.error(f"Group discard error: {e}")
        
        # 메트릭 업데이트
        self.metrics['connections'] -= 1
        
        logger.info(
            f"Connection closed: user={self.user.id}, "
            f"auction={self.auction_id}, code={close_code}, "
            f"last_seq={self.last_sequence}"
        )
    
    async def receive(self, text_data):
        """메시지 수신"""
        receive_time = time.time()
        
        try:
            data = json.loads(text_data)
            message_type = data.get('type')
            
            self.metrics['messages_received'] += 1
            
            # 메시지 타입별 처리
            if message_type == 'bid':
                await self.handle_bid(data)
            elif message_type == 'pong':
                await self.handle_pong(data)
            elif message_type == 'ack':
                await self.handle_ack(data)
            elif message_type == 'sync_request':
                await self.handle_sync_request(data)
            else:
                await self.send_error(f'Unknown message type: {message_type}')
            
            process_time = time.time() - receive_time
            if process_time > 0.1:  # 100ms 이상
                logger.warning(
                    f"Slow message processing: type={message_type}, "
                    f"time={process_time:.3f}s"
                )
                
        except json.JSONDecodeError:
            self.metrics['errors'] += 1
            await self.send_error('Invalid JSON format')
        except Exception as e:
            self.metrics['errors'] += 1
            logger.error(f"Receive error: {e}", exc_info=True)
            await self.send_error('Internal server error')
    
    async def handle_bid(self, data: Dict[str, Any]):
        """입찰 처리 with 재화 잠금"""
        try:
            bid_amount = int(data['amount'])
            
            if bid_amount <= 0:
                await self.send_error('Bid amount must be positive')
                return
            
            # 재화 잠금 먼저 시도 (Redis 분산 락)
            lock_acquired = await self.acquire_currency_lock(
                self.user.id,
                bid_amount
            )
            
            if not lock_acquired:
                await self.send_error('Insufficient currency or already locked')
                return
            
            try:
                # 입찰 처리
                result = await self.process_bid_with_retry(
                    self.auction_id,
                    self.user.id,
                    bid_amount
                )
                
                if result['success']:
                    # 시퀀스 증가 및 브로드캐스트
                    sequence = await self.increment_sequence_safe(self.auction_id)
                    
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
                    
                    # 히스토리 저장 (실패해도 계속 진행)
                    try:
                        await self.save_message_history_safe(
                            self.auction_id,
                            sequence,
                            message
                        )
                    except Exception as e:
                        logger.error(f"History save failed: {e}")
                    
                    # 브로드캐스트
                    await self.channel_layer.group_send(
                        self.auction_group_name,
                        {
                            'type': 'broadcast_message',
                            'message': message
                        }
                    )
                    
                    logger.info(
                        f"Bid successful: auction={self.auction_id}, "
                        f"user={self.user.id}, amount={bid_amount}, seq={sequence}"
                    )
                else:
                    # 입찰 실패 시 재화 잠금 해제
                    await self.release_currency_lock(self.user.id, bid_amount)
                    await self.send_error(result['error'])
                    
            except Exception as e:
                # 에러 발생 시 재화 잠금 해제
                await self.release_currency_lock(self.user.id, bid_amount)
                raise e
                
        except ValueError:
            await self.send_error('Invalid bid amount')
        except Exception as e:
            logger.error(f"Bid handling error: {e}", exc_info=True)
            await self.send_error('Failed to process bid')
    
    async def handle_pong(self, data: Dict[str, Any]):
        """Pong 응답 처리"""
        client_seq = data.get('sequence', 0)
        
        # 클라이언트가 뒤처져 있는지 확인
        current_seq = await self.get_current_sequence_safe(self.auction_id)
        
        if client_seq < current_seq - 10:  # 10개 이상 차이
            logger.warning(
                f"Client lagging: user={self.user.id}, "
                f"client_seq={client_seq}, server_seq={current_seq}"
            )
            await self.handle_reconnect(client_seq)
    
    async def handle_ack(self, data: Dict[str, Any]):
        """메시지 수신 확인 처리"""
        ack_seq = data.get('sequence')
        if ack_seq and ack_seq in self.pending_acks:
            self.pending_acks.remove(ack_seq)
            self.last_sequence = max(self.last_sequence, ack_seq)
    
    async def handle_sync_request(self, data: Dict[str, Any]):
        """동기화 요청 처리"""
        from_seq = data.get('from_sequence', self.last_sequence)
        await self.handle_reconnect(from_seq)
    
    async def handle_reconnect(self, last_seq: int):
        """재연결 처리 with fallback"""
        try:
            # Redis에서 메시지 히스토리 조회 시도
            missed_messages = await self.get_message_history_safe(
                self.auction_id,
                last_seq
            )
            
            if missed_messages:
                # 너무 많으면 일부만 전송
                if len(missed_messages) > 100:
                    logger.warning(
                        f"Too many missed messages: {len(missed_messages)}, "
                        f"sending only recent 100"
                    )
                    missed_messages = missed_messages[-100:]
                
                await self.send(text_data=json.dumps({
                    'type': 'reconnect_sync',
                    'missed_count': len(missed_messages),
                    'messages': missed_messages,
                    'truncated': len(missed_messages) == 100
                }))
            else:
                # 히스토리 없으면 현재 상태만 전송
                await self.send_initial_state()
                
        except Exception as e:
            logger.error(f"Reconnect error: {e}", exc_info=True)
            # Fallback: 현재 상태라도 전송
            await self.send_initial_state()
    
    async def send_initial_state(self):
        """초기 상태 전송"""
        state = await self.get_auction_state(self.auction_id)
        current_seq = await self.get_current_sequence_safe(self.auction_id)
        
        await self.send(text_data=json.dumps({
            'type': 'initial_state',
            'sequence': current_seq,
            'data': state
        }))
        
        self.last_sequence = current_seq
    
    async def broadcast_message(self, event):
        """메시지 브로드캐스트 (버퍼링)"""
        message = event['message']
        sequence = message['sequence']
        
        # 중복 체크
        if sequence <= self.last_sequence:
            return
        
        # 버퍼에 추가 (비동기 전송을 위해)
        self.message_buffer.append(message)
        
        # 버퍼가 너무 크면 경고
        if len(self.message_buffer) > 100:
            logger.warning(
                f"Message buffer overflow: {len(self.message_buffer)} messages"
            )
            self.is_healthy = False
    
    async def message_sender(self):
        """
        백그라운드 태스크: 버퍼의 메시지를 주기적으로 전송
        백프레셔(backpressure) 처리
        """
        try:
            while True:
                if self.message_buffer:
                    # 배치로 전송
                    batch = self.message_buffer[:10]  # 최대 10개씩
                    self.message_buffer = self.message_buffer[10:]
                    
                    for message in batch:
                        try:
                            await self.send(text_data=json.dumps(message))
                            self.metrics['messages_sent'] += 1
                            self.last_sequence = message['sequence']
                            self.pending_acks.add(message['sequence'])
                        except Exception as e:
                            logger.error(f"Message send error: {e}")
                            self.is_healthy = False
                
                await asyncio.sleep(0.05)  # 50ms마다 체크
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Message sender error: {e}", exc_info=True)
    
    async def keep_alive(self):
        """연결 유지"""
        try:
            while True:
                await asyncio.sleep(30)
                
                current_seq = await self.get_current_sequence_safe(self.auction_id)
                
                await self.send(text_data=json.dumps({
                    'type': 'ping',
                    'timestamp': datetime.now().isoformat(),
                    'sequence': current_seq,
                    'buffer_size': len(self.message_buffer)
                }))
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Keep-alive error: {e}")
    
    async def health_check(self):
        """헬스 체크"""
        try:
            while True:
                await asyncio.sleep(60)
                
                # 버퍼 크기 체크
                if len(self.message_buffer) > 50:
                    logger.warning(
                        f"Health check warning: buffer_size={len(self.message_buffer)}"
                    )
                
                # Pending ACK 체크
                if len(self.pending_acks) > 20:
                    logger.warning(
                        f"Health check warning: pending_acks={len(self.pending_acks)}"
                    )
                
                # 건강하지 않으면 재연결 제안
                if not self.is_healthy:
                    await self.send(text_data=json.dumps({
                        'type': 'health_warning',
                        'message': 'Connection degraded, consider reconnecting'
                    }))
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Health check error: {e}")
    
    # Redis 작업 with Circuit Breaker
    
    async def get_current_sequence_safe(self, auction_id: str) -> int:
        """Circuit Breaker로 보호된 시퀀스 조회"""
        try:
            return await self.redis_circuit_breaker.call(
                self._get_current_sequence,
                auction_id
            )
        except Exception as e:
            logger.error(f"Sequence fetch failed, using fallback: {e}")
            return self.last_sequence  # Fallback
    
    async def _get_current_sequence(self, auction_id: str) -> int:
        """실제 시퀀스 조회"""
        async with RedisConnectionPool.get_connection() as redis:
            key = f'auction:{auction_id}:sequence'
            seq = await redis.get(key)
            return int(seq) if seq else 0
    
    async def increment_sequence_safe(self, auction_id: str) -> int:
        """Circuit Breaker로 보호된 시퀀스 증가"""
        return await self.redis_circuit_breaker.call(
            self._increment_sequence,
            auction_id
        )
    
    async def _increment_sequence(self, auction_id: str) -> int:
        """실제 시퀀스 증가"""
        async with RedisConnectionPool.get_connection() as redis:
            key = f'auction:{auction_id}:sequence'
            pipeline = redis.pipeline()
            pipeline.incr(key)
            pipeline.expire(key, 86400)
            results = await pipeline.execute()
            return results[0]
    
    async def save_message_history_safe(
        self,
        auction_id: str,
        sequence: int,
        message: dict
    ):
        """Circuit Breaker로 보호된 히스토리 저장"""
        try:
            await self.redis_circuit_breaker.call(
                self._save_message_history,
                auction_id,
                sequence,
                message
            )
        except Exception as e:
            logger.error(f"History save failed: {e}")
            # 실패해도 계속 진행 (히스토리는 필수가 아님)
    
    async def _save_message_history(
        self,
        auction_id: str,
        sequence: int,
        message: dict
    ):
        """실제 히스토리 저장"""
        async with RedisConnectionPool.get_connection() as redis:
            key = f'auction:{auction_id}:history'
            pipeline = redis.pipeline()
            pipeline.zadd(key, {json.dumps(message): sequence})
            pipeline.expire(key, 3600)
            pipeline.zremrangebyrank(key, 0, -1001)
            await pipeline.execute()
    
    async def get_message_history_safe(
        self,
        auction_id: str,
        after_seq: int
    ) -> list:
        """Circuit Breaker로 보호된 히스토리 조회"""
        try:
            return await self.redis_circuit_breaker.call(
                self._get_message_history,
                auction_id,
                after_seq
            )
        except Exception as e:
            logger.error(f"History fetch failed: {e}")
            return []  # 실패 시 빈 리스트 반환
    
    async def _get_message_history(
        self,
        auction_id: str,
        after_seq: int
    ) -> list:
        """실제 히스토리 조회"""
        async with RedisConnectionPool.get_connection() as redis:
            key = f'auction:{auction_id}:history'
            messages = await redis.zrangebyscore(
                key,
                after_seq + 1,
                '+inf'
            )
            return [json.loads(msg) for msg in messages]
    
    # 재화 잠금 (분산 락)
    
    async def acquire_currency_lock(self, user_id: int, amount: int) -> bool:
        """재화 잠금 획득"""
        try:
            async with RedisConnectionPool.get_connection() as redis:
                lock_key = f'currency_lock:{user_id}'
                
                # SET NX EX로 원자적 락 획득
                acquired = await redis.set(
                    lock_key,
                    amount,
                    nx=True,  # key가 없을 때만 설정
                    ex=30     # 30초 후 자동 해제
                )
                
                return acquired is not None
        except Exception as e:
            logger.error(f"Lock acquire error: {e}")
            return False
    
    async def release_currency_lock(self, user_id: int, amount: int):
        """재화 잠금 해제"""
        try:
            async with RedisConnectionPool.get_connection() as redis:
                lock_key = f'currency_lock:{user_id}'
                await redis.delete(lock_key)
        except Exception as e:
            logger.error(f"Lock release error: {e}")
    
    # Rate limiting
    
    async def check_rate_limit(self) -> bool:
        """Rate limiting 체크"""
        try:
            async with RedisConnectionPool.get_connection() as redis:
                key = f'rate_limit:{self.user.id}'
                
                # Sliding window rate limit
                now = time.time()
                window = 60  # 1분
                limit = 100  # 1분에 100개 요청
                
                pipeline = redis.pipeline()
                pipeline.zadd(key, {str(now): now})
                pipeline.zremrangebyscore(key, 0, now - window)
                pipeline.zcard(key)
                pipeline.expire(key, window)
                
                results = await pipeline.execute()
                count = results[2]
                
                return count <= limit
        except Exception as e:
            logger.error(f"Rate limit check error: {e}")
            return True  # 실패 시 허용 (안전한 쪽으로)
    
    async def process_bid_with_retry(
        self,
        auction_id: str,
        user_id: int,
        amount: int,
        max_retries: int = 3
    ) -> Dict[str, Any]:
        """재시도 로직이 있는 입찰 처리"""
        for attempt in range(max_retries):
            try:
                return await self.process_bid(auction_id, user_id, amount)
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                logger.warning(f"Bid attempt {attempt + 1} failed, retrying: {e}")
                await asyncio.sleep(0.1 * (attempt + 1))  # Exponential backoff
    
    # Helper 메서드들
    
    def _parse_query_string(self, query_string: str) -> Dict[str, str]:
        """쿼리 스트링 파싱"""
        if not query_string:
            return {}
        return dict(
            param.split('=') for param in query_string.split('&')
            if '=' in param
        )
    
    async def send_error(self, message: str):
        """에러 메시지 전송"""
        await self.send(text_data=json.dumps({
            'type': 'error',
            'message': message,
            'timestamp': datetime.now().isoformat()
        }))
    
    # Django ORM 작업들
    
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
                auction = Auction.objects.select_for_update(
                    nowait=False,  # 락 대기
                    skip_locked=False
                ).get(id=auction_id)
                
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
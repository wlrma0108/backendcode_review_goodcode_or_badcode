# Level 4: Redis 분산 락 + DB 트랜잭션
# 개선: 애플리케이션 레벨 락으로 성능 향상

import redis
import uuid
from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from contextlib import contextmanager
import logging

logger = logging.getLogger(__name__)

# Redis 클라이언트
redis_client = redis.StrictRedis(
    host='localhost',
    port=6379,
    db=0,
    decode_responses=True
)


@contextmanager
def acquire_redis_lock(key, timeout=10):
    """Redis 분산 락 컨텍스트 매니저"""
    lock_id = str(uuid.uuid4())
    
    # 락 획득 시도
    acquired = redis_client.set(
        key,
        lock_id,
        nx=True,  # key가 없을 때만
        ex=timeout  # 타임아웃
    )
    
    if not acquired:
        raise Exception("Lock acquisition failed")
    
    try:
        yield lock_id
    finally:
        # Lua 스크립트로 안전하게 해제 (내가 잠근 것만)
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        redis_client.eval(lua_script, 1, key, lock_id)


class BidService:
    """비즈니스 로직 분리"""
    
    @staticmethod
    def place_bid(user, auction_id, amount):
        # 1. Redis 락으로 사용자별 직렬화
        user_lock_key = f'bid_lock:user:{user.id}'
        
        try:
            with acquire_redis_lock(user_lock_key, timeout=5):
                # 2. DB 트랜잭션 (짧게 유지)
                with transaction.atomic():
                    # Currency 조회 (select_for_update는 여전히 필요)
                    currency = Currency.objects.select_for_update(
                        nowait=False  # 대기
                    ).get(user=user)
                    
                    if currency.available_balance < amount:
                        raise ValueError('Insufficient balance')
                    
                    # Auction 조회
                    auction = Auction.objects.select_for_update(
                        nowait=False
                    ).get(id=auction_id)
                    
                    if auction.status != 'active':
                        raise ValueError('Auction not active')
                    
                    if amount <= auction.current_price:
                        raise ValueError(f'Bid must be higher than {auction.current_price}')
                    
                    # 이전 입찰자 정보 저장
                    previous_winner = auction.current_winner
                    previous_amount = auction.current_price
                    
                    # 현재 입찰자 재화 잠금
                    currency.balance -= amount
                    currency.locked_balance += amount
                    currency.save()
                    
                    # 입찰 생성
                    bid = Bid.objects.create(
                        auction=auction,
                        user=user,
                        amount=amount
                    )
                    
                    # 경매 업데이트
                    auction.current_price = amount
                    auction.current_winner = user
                    auction.save()
                
                # 3. 이전 입찰자 처리 (별도 트랜잭션)
                if previous_winner:
                    BidService._release_previous_bid(
                        previous_winner,
                        auction_id,
                        previous_amount
                    )
                
                logger.info(f"Bid placed: user={user.id}, auction={auction_id}, amount={amount}")
                return {'success': True, 'bid_id': bid.id}
                
        except redis.exceptions.LockError:
            raise ValueError('Another bid in progress')
        except Exception as e:
            logger.error(f"Bid error: {e}", exc_info=True)
            raise
    
    @staticmethod
    def _release_previous_bid(user, auction_id, amount):
        """이전 입찰자 재화 해제"""
        try:
            with transaction.atomic():
                currency = Currency.objects.select_for_update().get(user=user)
                currency.balance += amount
                currency.locked_balance -= amount
                currency.save()
                
            logger.info(f"Released previous bid: user={user.id}, amount={amount}")
        except Exception as e:
            logger.error(f"Failed to release previous bid: {e}")
            # 실패해도 메인 입찰은 성공으로 처리
            # 백그라운드 재시도 필요 (Celery)


class BidCreateView(APIView):
    def post(self, request, auction_id):
        user = request.user
        amount = request.data.get('amount')
        
        try:
            result = BidService.place_bid(user, auction_id, amount)
            return Response(result)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': 'Internal error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


"""
개선점:
✓ Redis 락으로 사용자별 직렬화
✓ DB 트랜잭션 짧게 유지 (성능 향상)
✓ 타임아웃 관리 (5초)
✓ 안전한 락 해제 (Lua 스크립트)
✓ Service Layer 분리

동작 원리:
User A가 경매 1, 2에 동시 입찰:
  경매 1: Redis lock:user:A 획득 → 처리
  경매 2: Redis lock:user:A 대기 → 순차 처리
  
User A와 User B가 경매 1에 동시 입찰:
  User A: Redis lock:user:A 획득 → 처리
  User B: Redis lock:user:B 획득 → 처리 (동시 가능!)
  
  DB 레벨에서:
    Thread A: auction 1 select_for_update
    Thread B: auction 1 select_for_update 대기
    → 순차 처리

장점:
1. 성능 향상:
   - 사용자별 직렬화 (경매별 X)
   - DB 락 시간 최소화
   
2. 확장성:
   - Redis 기반 (분산 환경 OK)
   - DB 부하 감소

3. 타임아웃:
   - 5초 후 자동 해제
   - 데드락 방지

문제점:

1. Redis 장애:
   - Redis 다운 시 전체 입찰 불가
   - Circuit Breaker 필요

2. 복잡도:
   - Redis + DB 두 개 관리
   - 디버깅 어려움

3. 이전 입찰자 해제 실패:
   - 별도 트랜잭션이라 실패 가능
   - 재시도 메커니즘 필요 (Celery)
"""

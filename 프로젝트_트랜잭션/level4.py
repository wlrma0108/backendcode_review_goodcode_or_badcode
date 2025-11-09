# services.py
import redis
import uuid
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)

# Redis 클라이언트
redis_client = redis.StrictRedis(
    host='localhost',
    port=6379,
    db=0,
    decode_responses=True
)


class CurrencyLockService:
 
    LOCK_TIMEOUT = 300  # 5분
    
    @staticmethod
    def _get_lock_key(user_id: int, auction_id: int) -> str:
        """Redis 락 키 생성"""
        return f"currency_lock:{user_id}:{auction_id}"
    
    @staticmethod
    def _get_user_lock_key(user_id: int) -> str:
        """사용자별 락 키"""
        return f"user_lock:{user_id}"
    
    @staticmethod
    def acquire_currency_lock(user_id: int, auction_id: int, amount: int) -> dict:
        """
        재화 잠금 시도
        
        Returns:
            {
                'success': bool,
                'lock_id': str or None,
                'error': str or None
            }
        """
        try:
            # 1. 사용자 레벨 락 (동시 입찰 방지)
            user_lock_key = CurrencyLockService._get_user_lock_key(user_id)
            lock_id = str(uuid.uuid4())
            
            # SET NX EX로 원자적 락 획득
            acquired = redis_client.set(
                user_lock_key,
                lock_id,
                nx=True,
                ex=10  # 10초 타임아웃
            )
            
            if not acquired:
                return {
                    'success': False,
                    'lock_id': None,
                    'error': 'Another operation in progress'
                }
            
            try:
                # 2. DB에서 재화 확인
                from .models import UserCurrency
                
                user_currency = UserCurrency.objects.get(user_id=user_id)
                
                if user_currency.available_amount < amount:
                    return {
                        'success': False,
                        'lock_id': None,
                        'error': f'Insufficient currency. Available: {user_currency.available_amount}'
                    }
                
                # 3. Redis에 잠금 정보 저장
                lock_key = CurrencyLockService._get_lock_key(user_id, auction_id)
                lock_data = {
                    'amount': amount,
                    'locked_at': timezone.now().isoformat(),
                    'lock_id': lock_id
                }
                
                redis_client.hmset(lock_key, lock_data)
                redis_client.expire(lock_key, CurrencyLockService.LOCK_TIMEOUT)
                
                # 4. DB에 잠금 증가 (별도 트랜잭션)
                with transaction.atomic():
                    user_currency = UserCurrency.objects.select_for_update().get(
                        user_id=user_id
                    )
                    user_currency.locked_amount += amount
                    user_currency.save()
                
                logger.info(f"Currency locked: user={user_id}, amount={amount}")
                
                return {
                    'success': True,
                    'lock_id': lock_id,
                    'error': None
                }
                
            finally:
                # 사용자 락 해제
                # Lua 스크립트로 원자적 해제 (내가 잠근 것만 해제)
                lua_script = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("del", KEYS[1])
                else
                    return 0
                end
                """
                redis_client.eval(lua_script, 1, user_lock_key, lock_id)
                
        except Exception as e:
            logger.error(f"Currency lock error: {e}", exc_info=True)
            return {
                'success': False,
                'lock_id': None,
                'error': str(e)
            }
    
    @staticmethod
    def release_currency_lock(user_id: int, auction_id: int, lock_id: str) -> bool:
        """
        재화 잠금 해제 (보상 트랜잭션)
        
        멱등성 보장: 여러 번 호출해도 안전
        """
        try:
            lock_key = CurrencyLockService._get_lock_key(user_id, auction_id)
            
            # 1. Redis에서 잠금 정보 조회
            lock_data = redis_client.hgetall(lock_key)
            
            if not lock_data:
                # 이미 해제됐거나 만료됨
                logger.warning(f"Lock not found: user={user_id}, auction={auction_id}")
                return True
            
            if lock_data.get('lock_id') != lock_id:
                logger.error(f"Lock ID mismatch: expected={lock_id}, got={lock_data.get('lock_id')}")
                return False
            
            amount = int(lock_data['amount'])
            
            # 2. DB에서 잠금 감소
            with transaction.atomic():
                from .models import UserCurrency, CurrencyLock
                
                user_currency = UserCurrency.objects.select_for_update().get(
                    user_id=user_id
                )
                
                # 안전 장치: locked_amount가 음수 되지 않도록
                if user_currency.locked_amount >= amount:
                    user_currency.locked_amount -= amount
                    user_currency.save()
                else:
                    logger.error(
                        f"Locked amount mismatch: user={user_id}, "
                        f"locked={user_currency.locked_amount}, release={amount}"
                    )
                
                # 3. CurrencyLock 기록 업데이트
                CurrencyLock.objects.filter(
                    user_id=user_id,
                    auction_id=auction_id,
                    status='locked'
                ).update(status='released')
            
            # 4. Redis 정리
            redis_client.delete(lock_key)
            
            logger.info(f"Currency unlocked: user={user_id}, amount={amount}")
            return True
            
        except Exception as e:
            logger.error(f"Currency unlock error: {e}", exc_info=True)
            return False
    
    @staticmethod
    def consume_currency_lock(user_id: int, auction_id: int, lock_id: str) -> bool:
        """
        재화 잠금 확정 (실제 차감)
        
        경매 낙찰 시 호출
        """
        try:
            lock_key = CurrencyLockService._get_lock_key(user_id, auction_id)
            lock_data = redis_client.hgetall(lock_key)
            
            if not lock_data:
                logger.error(f"Lock not found for consumption: user={user_id}")
                return False
            
            if lock_data.get('lock_id') != lock_id:
                logger.error(f"Lock ID mismatch for consumption")
                return False
            
            amount = int(lock_data['amount'])
            
            # DB에서 실제 차감
            with transaction.atomic():
                from .models import UserCurrency, CurrencyLock
                
                user_currency = UserCurrency.objects.select_for_update().get(
                    user_id=user_id
                )
                
                # 전체 재화 차감 + 잠금 해제
                user_currency.total_amount -= amount
                user_currency.locked_amount -= amount
                user_currency.save()
                
                # CurrencyLock 기록 업데이트
                CurrencyLock.objects.filter(
                    user_id=user_id,
                    auction_id=auction_id,
                    status='locked'
                ).update(status='consumed')
            
            # Redis 정리
            redis_client.delete(lock_key)
            
            logger.info(f"Currency consumed: user={user_id}, amount={amount}")
            return True
            
        except Exception as e:
            logger.error(f"Currency consume error: {e}", exc_info=True)
            return False


class BidService:

    
    @staticmethod
    def place_bid(user_id: int, auction_id: int, bid_amount: int) -> dict:
  
        lock_id = None
        previous_winner_id = None
        previous_amount = None
        previous_lock_id = None
        
        try:
            # === 단계 1: 재화 잠금 ===
            lock_result = CurrencyLockService.acquire_currency_lock(
                user_id,
                auction_id,
                bid_amount
            )
            
            if not lock_result['success']:
                return {
                    'success': False,
                    'error': lock_result['error'],
                    'step': 'currency_lock'
                }
            
            lock_id = lock_result['lock_id']
            
            # === 단계 2: 경매 검증 및 업데이트 ===
            try:
                with transaction.atomic():
                    from .models import Auction, Bid, CurrencyLock as CurrencyLockModel
                    
                    # 경매 조회 (락)
                    auction = Auction.objects.select_for_update().get(
                        id=auction_id
                    )
                    
                    # 검증
                    if auction.status != 'active':
                        raise Exception('Auction is not active')
                    
                    if bid_amount <= auction.current_price:
                        raise Exception(
                            f'Bid must be higher than {auction.current_price}'
                        )
                    
                    # 이전 최고 입찰자 정보 저장
                    if auction.current_winner_id:
                        previous_winner_id = auction.current_winner_id
                        previous_amount = auction.current_price
                        
                        # 이전 입찰자의 lock_id 조회
                        try:
                            prev_lock = CurrencyLockModel.objects.get(
                                user_id=previous_winner_id,
                                auction_id=auction_id,
                                status='locked'
                            )
                            previous_lock_id = prev_lock.lock_id
                        except CurrencyLockModel.DoesNotExist:
                            logger.warning(
                                f"Previous lock not found: "
                                f"user={previous_winner_id}, auction={auction_id}"
                            )
                    
                    # 입찰 기록 생성
                    bid = Bid.objects.create(
                        auction=auction,
                        user_id=user_id,
                        amount=bid_amount,
                        is_winning=True
                    )
                    
                    # CurrencyLock 기록 생성
                    CurrencyLockModel.objects.create(
                        user_id=user_id,
                        auction_id=auction_id,
                        amount=bid_amount,
                        status='locked',
                        lock_id=lock_id
                    )
                    
                    # 경매 업데이트
                    auction.current_price = bid_amount
                    auction.current_winner_id = user_id
                    auction.save()
                    
                    logger.info(
                        f"Bid placed: user={user_id}, "
                        f"auction={auction_id}, amount={bid_amount}"
                    )
                
                # === 단계 3: 이전 입찰자 잠금 해제 (비동기로 처리 가능) ===
                if previous_winner_id and previous_lock_id:
                    # 이전 입찰자 재화 해제
                    release_success = CurrencyLockService.release_currency_lock(
                        previous_winner_id,
                        auction_id,
                        previous_lock_id
                    )
                    
                    if not release_success:
                        # 실패해도 입찰은 성공으로 간주
                        # 백그라운드 작업으로 재시도
                        logger.error(
                            f"Failed to release previous winner lock: "
                            f"user={previous_winner_id}"
                        )
                
                return {
                    'success': True,
                    'bid_id': bid.id,
                    'lock_id': lock_id
                }
                
            except Exception as e:
                # === 보상 트랜잭션: 재화 잠금 해제 ===
                logger.error(f"Bid placement failed: {e}", exc_info=True)
                
                if lock_id:
                    CurrencyLockService.release_currency_lock(
                        user_id,
                        auction_id,
                        lock_id
                    )
                
                return {
                    'success': False,
                    'error': str(e),
                    'step': 'bid_placement'
                }
                
        except Exception as e:
            logger.error(f"Unexpected error in place_bid: {e}", exc_info=True)
            
            # 보상 트랜잭션
            if lock_id:
                CurrencyLockService.release_currency_lock(
                    user_id,
                    auction_id,
                    lock_id
                )
            
            return {
                'success': False,
                'error': str(e),
                'step': 'unexpected'
            }


# views.py
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(['POST'])
def place_bid_v4(request, auction_id):

    user = request.user
    bid_amount = request.data.get('amount')
    
    if not bid_amount or bid_amount <= 0:
        return Response({
            'success': False,
            'error': 'Invalid bid amount'
        }, status=400)
    
    result = BidService.place_bid(
        user.id,
        auction_id,
        bid_amount
    )
    
    if result['success']:
        return Response(result)
    else:
        return Response(result, status=400)

# services/currency_service.py
import redis
import uuid
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
import logging
from typing import Optional, Dict, Any
from contextlib import contextmanager
import time

logger = logging.getLogger(__name__)

# Redis 클라이언트 (연결 풀 사용)
redis_pool = redis.ConnectionPool(
    host='localhost',
    port=6379,
    db=0,
    decode_responses=True,
    max_connections=50
)
redis_client = redis.StrictRedis(connection_pool=redis_pool)


class CircuitBreaker:
    """Redis 장애 대응용 Circuit Breaker"""
    def __init__(self, failure_threshold=5, timeout=60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failures = 0
        self.last_failure_time = None
        self.state = 'closed'  # closed, open, half_open
    
    def call(self, func, *args, **kwargs):
        if self.state == 'open':
            if time.time() - self.last_failure_time > self.timeout:
                self.state = 'half_open'
            else:
                raise Exception("Circuit breaker is open - Redis unavailable")
        
        try:
            result = func(*args, **kwargs)
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


redis_circuit_breaker = CircuitBreaker()


class CurrencyLockMetrics:
    """재화 잠금 메트릭 수집"""
    
    @staticmethod
    def record_lock_attempt(success: bool):
        """잠금 시도 기록"""
        key = 'metrics:currency_lock:attempts'
        redis_client.hincrby(key, 'total', 1)
        if success:
            redis_client.hincrby(key, 'success', 1)
        else:
            redis_client.hincrby(key, 'failure', 1)
    
    @staticmethod
    def record_lock_duration(duration_ms: float):
        """잠금 유지 시간 기록"""
        # Histogram-like storage
        bucket = int(duration_ms / 100)  # 100ms 버킷
        key = f'metrics:currency_lock:duration:{bucket}'
        redis_client.incr(key)
        redis_client.expire(key, 3600)  # 1시간
    
    @staticmethod
    def get_metrics() -> Dict[str, Any]:
        """메트릭 조회"""
        attempts = redis_client.hgetall('metrics:currency_lock:attempts')
        return {
            'attempts': {
                'total': int(attempts.get('total', 0)),
                'success': int(attempts.get('success', 0)),
                'failure': int(attempts.get('failure', 0)),
            }
        }


class CurrencyLockService:
 
    
    LOCK_TIMEOUT = 300  # 5분
    RETRY_ATTEMPTS = 3
    RETRY_DELAY = 0.1  # 100ms
    
    @staticmethod
    def _get_lock_key(user_id: int, auction_id: int) -> str:
        return f"currency_lock:{user_id}:{auction_id}"
    
    @staticmethod
    def _get_user_lock_key(user_id: int) -> str:
        return f"user_lock:{user_id}"
    
    @staticmethod
    @contextmanager
    def _acquire_user_lock(user_id: int, timeout: int = 10):
        """
        사용자 레벨 락 컨텍스트 매니저
        
        자동으로 획득/해제 처리
        """
        lock_key = CurrencyLockService._get_user_lock_key(user_id)
        lock_id = str(uuid.uuid4())
        
        acquired = False
        try:
            # 락 획득 재시도
            for attempt in range(CurrencyLockService.RETRY_ATTEMPTS):
                acquired = redis_circuit_breaker.call(
                    redis_client.set,
                    lock_key,
                    lock_id,
                    nx=True,
                    ex=timeout
                )
                
                if acquired:
                    break
                
                if attempt < CurrencyLockService.RETRY_ATTEMPTS - 1:
                    time.sleep(CurrencyLockService.RETRY_DELAY * (attempt + 1))
            
            if not acquired:
                raise Exception(f"Failed to acquire user lock after {CurrencyLockService.RETRY_ATTEMPTS} attempts")
            
            yield lock_id
            
        finally:
            if acquired:
                # Lua 스크립트로 안전하게 해제
                lua_script = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("del", KEYS[1])
                else
                    return 0
                end
                """
                try:
                    redis_circuit_breaker.call(
                        redis_client.eval,
                        lua_script,
                        1,
                        lock_key,
                        lock_id
                    )
                except Exception as e:
                    logger.error(f"Failed to release user lock: {e}")
    
    @staticmethod
    def acquire_currency_lock(
        user_id: int,
        auction_id: int,
        amount: int
    ) -> Dict[str, Any]:
        """
        재화 잠금 획득
        
        Returns:
            {
                'success': bool,
                'lock_id': str or None,
                'error': str or None,
                'available_amount': int or None
            }
        """
        start_time = time.time()
        lock_id = str(uuid.uuid4())
        
        try:
            # 1. 사용자 레벨 락 획득
            with CurrencyLockService._acquire_user_lock(user_id):
                
                # 2. DB에서 재화 확인
                from .models import UserCurrency
                
                try:
                    user_currency = UserCurrency.objects.get(user_id=user_id)
                except UserCurrency.DoesNotExist:
                    CurrencyLockMetrics.record_lock_attempt(False)
                    return {
                        'success': False,
                        'lock_id': None,
                        'error': 'User currency not found',
                        'available_amount': None
                    }
                
                available = user_currency.available_amount
                
                if available < amount:
                    CurrencyLockMetrics.record_lock_attempt(False)
                    return {
                        'success': False,
                        'lock_id': None,
                        'error': f'Insufficient currency',
                        'available_amount': available
                    }
                
                # 3. Redis에 잠금 정보 저장
                lock_key = CurrencyLockService._get_lock_key(user_id, auction_id)
                lock_data = {
                    'amount': amount,
                    'locked_at': timezone.now().isoformat(),
                    'lock_id': lock_id,
                    'user_id': user_id,
                    'auction_id': auction_id
                }
                
                try:
                    redis_circuit_breaker.call(
                        redis_client.hmset,
                        lock_key,
                        lock_data
                    )
                    redis_circuit_breaker.call(
                        redis_client.expire,
                        lock_key,
                        CurrencyLockService.LOCK_TIMEOUT
                    )
                except Exception as e:
                    logger.error(f"Redis lock creation failed: {e}")
                    # Redis 실패해도 DB 잠금은 진행 (Graceful degradation)
                
                # 4. DB에 잠금 증가
                try:
                    with transaction.atomic():
                        user_currency = UserCurrency.objects.select_for_update(
                            nowait=False  # 대기
                        ).get(user_id=user_id)
                        
                        # 재확인 (다른 트랜잭션이 끼어들 수 있음)
                        if user_currency.available_amount < amount:
                            raise Exception('Insufficient currency after re-check')
                        
                        user_currency.locked_amount += amount
                        user_currency.save()
                        
                        # CurrencyLock 기록 생성
                        from .models import CurrencyLock
                        CurrencyLock.objects.create(
                            user_id=user_id,
                            auction_id=auction_id,
                            amount=amount,
                            status='locked',
                            lock_id=lock_id
                        )
                
                except Exception as e:
                    logger.error(f"DB lock creation failed: {e}", exc_info=True)
                    
                    # Redis 정리 (보상 트랜잭션)
                    try:
                        redis_client.delete(lock_key)
                    except:
                        pass
                    
                    CurrencyLockMetrics.record_lock_attempt(False)
                    return {
                        'success': False,
                        'lock_id': None,
                        'error': str(e),
                        'available_amount': available
                    }
                
                # 5. 성공
                duration_ms = (time.time() - start_time) * 1000
                CurrencyLockMetrics.record_lock_attempt(True)
                CurrencyLockMetrics.record_lock_duration(duration_ms)
                
                logger.info(
                    f"Currency locked: user={user_id}, auction={auction_id}, "
                    f"amount={amount}, lock_id={lock_id}, duration={duration_ms:.2f}ms"
                )
                
                return {
                    'success': True,
                    'lock_id': lock_id,
                    'error': None,
                    'available_amount': available - amount
                }
                
        except Exception as e:
            logger.error(f"Currency lock error: {e}", exc_info=True)
            CurrencyLockMetrics.record_lock_attempt(False)
            return {
                'success': False,
                'lock_id': None,
                'error': str(e),
                'available_amount': None
            }
    
    @staticmethod
    def release_currency_lock(
        user_id: int,
        auction_id: int,
        lock_id: str,
        retry: bool = True
    ) -> bool:
        """
        재화 잠금 해제
        
        멱등성 보장: 여러 번 호출해도 안전
        """
        try:
            lock_key = CurrencyLockService._get_lock_key(user_id, auction_id)
            
            # 1. Redis에서 잠금 정보 조회
            try:
                lock_data = redis_circuit_breaker.call(
                    redis_client.hgetall,
                    lock_key
                )
            except Exception as e:
                logger.error(f"Redis fetch failed during unlock: {e}")
                lock_data = {}
            
            # 2. DB에서 잠금 정보 조회 (Redis 실패 시 대비)
            from .models import CurrencyLock
            
            try:
                db_lock = CurrencyLock.objects.get(
                    user_id=user_id,
                    auction_id=auction_id,
                    lock_id=lock_id
                )
                amount = db_lock.amount
                
                if db_lock.status != 'locked':
                    logger.info(f"Lock already released: {lock_id}")
                    return True
                
            except CurrencyLock.DoesNotExist:
                logger.warning(f"Lock not found in DB: {lock_id}")
                
                # Redis에 정보가 있으면 사용
                if lock_data:
                    amount = int(lock_data['amount'])
                else:
                    return True  # 이미 처리됨
            
            # 3. DB 잠금 해제
            with transaction.atomic():
                from .models import UserCurrency
                
                user_currency = UserCurrency.objects.select_for_update().get(
                    user_id=user_id
                )
                
                # 안전 장치
                if user_currency.locked_amount >= amount:
                    user_currency.locked_amount -= amount
                    user_currency.save()
                else:
                    logger.error(
                        f"Locked amount mismatch: user={user_id}, "
                        f"locked={user_currency.locked_amount}, release={amount}"
                    )
                    # 어쨌든 0으로 맞춤
                    user_currency.locked_amount = max(0, user_currency.locked_amount - amount)
                    user_currency.save()
                
                # CurrencyLock 상태 업데이트
                CurrencyLock.objects.filter(
                    user_id=user_id,
                    auction_id=auction_id,
                    lock_id=lock_id,
                    status='locked'
                ).update(status='released')
            
            # 4. Redis 정리
            try:
                redis_client.delete(lock_key)
            except Exception as e:
                logger.error(f"Redis cleanup failed: {e}")
            
            logger.info(f"Currency unlocked: user={user_id}, amount={amount}, lock_id={lock_id}")
            return True
            
        except Exception as e:
            logger.error(f"Currency unlock error: {e}", exc_info=True)
            
            # 재시도 큐에 추가
            if retry:
                from .tasks import retry_release_lock
                retry_release_lock.apply_async(
                    args=[user_id, auction_id, lock_id],
                    countdown=60  # 1분 후 재시도
                )
            
            return False
    
    @staticmethod
    def consume_currency_lock(
        user_id: int,
        auction_id: int,
        lock_id: str
    ) -> bool:
        """
        재화 잠금 확정 (실제 차감)
        """
        try:
            from .models import UserCurrency, CurrencyLock
            
            # DB에서 잠금 정보 조회
            try:
                db_lock = CurrencyLock.objects.get(
                    user_id=user_id,
                    auction_id=auction_id,
                    lock_id=lock_id
                )
            except CurrencyLock.DoesNotExist:
                logger.error(f"Lock not found for consumption: {lock_id}")
                return False
            
            if db_lock.status == 'consumed':
                logger.info(f"Lock already consumed: {lock_id}")
                return True
            
            amount = db_lock.amount
            
            # DB 트랜잭션
            with transaction.atomic():
                user_currency = UserCurrency.objects.select_for_update().get(
                    user_id=user_id
                )
                
                # 재화 차감
                user_currency.total_amount -= amount
                user_currency.locked_amount -= amount
                
                # 음수 방지
                if user_currency.total_amount < 0:
                    logger.error(
                        f"Currency would go negative: user={user_id}, "
                        f"total={user_currency.total_amount}, consume={amount}"
                    )
                    raise Exception("Insufficient total currency")
                
                user_currency.save()
                
                # 상태 업데이트
                db_lock.status = 'consumed'
                db_lock.save()
            
            # Redis 정리
            lock_key = CurrencyLockService._get_lock_key(user_id, auction_id)
            try:
                redis_client.delete(lock_key)
            except:
                pass
            
            logger.info(
                f"Currency consumed: user={user_id}, amount={amount}, lock_id={lock_id}"
            )
            return True
            
        except Exception as e:
            logger.error(f"Currency consume error: {e}", exc_info=True)
            return False


class BidService:
    """프로덕션급 입찰 서비스"""
    
    @staticmethod
    def place_bid(
        user_id: int,
        auction_id: int,
        bid_amount: int
    ) -> Dict[str, Any]:
        """
        입찰 처리
        
        단계:
        1. 재화 잠금
        2. 경매 검증 및 업데이트
        3. 이전 입찰자 잠금 해제 (비동기)
        4. 알림 발송 (비동기)
        """
        lock_id = None
        
        try:
            # === 1. 재화 잠금 ===
            lock_result = CurrencyLockService.acquire_currency_lock(
                user_id,
                auction_id,
                bid_amount
            )
            
            if not lock_result['success']:
                return {
                    'success': False,
                    'error': lock_result['error'],
                    'available_amount': lock_result['available_amount'],
                    'step': 'currency_lock'
                }
            
            lock_id = lock_result['lock_id']
            
            # === 2. 경매 처리 ===
            try:
                with transaction.atomic():
                    from .models import Auction, Bid, CurrencyLock as CurrencyLockModel
                    
                    # 경매 조회
                    auction = Auction.objects.select_for_update(
                        nowait=False
                    ).get(id=auction_id)
                    
                    # 검증
                    if auction.status != 'active':
                        raise Exception('Auction is not active')
                    
                    if bid_amount <= auction.current_price:
                        raise Exception(
                            f'Bid must be higher than {auction.current_price}'
                        )
                    
                    # 이전 최고 입찰자 정보
                    previous_winner_id = auction.current_winner_id
                    previous_lock_id = None
                    
                    if previous_winner_id:
                        try:
                            prev_lock = CurrencyLockModel.objects.get(
                                user_id=previous_winner_id,
                                auction_id=auction_id,
                                status='locked'
                            )
                            previous_lock_id = prev_lock.lock_id
                        except CurrencyLockModel.DoesNotExist:
                            logger.warning(
                                f"Previous lock not found: user={previous_winner_id}"
                            )
                    
                    # 입찰 생성
                    bid = Bid.objects.create(
                        auction=auction,
                        user_id=user_id,
                        amount=bid_amount,
                        is_winning=True
                    )
                    
                    # 이전 입찰 상태 업데이트
                    if previous_winner_id:
                        Bid.objects.filter(
                            auction=auction,
                            user_id=previous_winner_id,
                            is_winning=True
                        ).update(is_winning=False)
                    
                    # 경매 업데이트
                    auction.current_price = bid_amount
                    auction.current_winner_id = user_id
                    auction.save()
                    
                    logger.info(
                        f"Bid placed successfully: user={user_id}, "
                        f"auction={auction_id}, amount={bid_amount}, bid_id={bid.id}"
                    )
                
                # === 3. 이전 입찰자 처리 (비동기) ===
                if previous_winner_id and previous_lock_id:
                    from .tasks import release_previous_lock
                    release_previous_lock.apply_async(
                        args=[previous_winner_id, auction_id, previous_lock_id],
                        countdown=1  # 1초 후
                    )
                
                # === 4. 알림 발송 (비동기) ===
                from .tasks import send_bid_notifications
                send_bid_notifications.apply_async(
                    args=[auction_id, user_id, bid_amount]
                )
                
                return {
                    'success': True,
                    'bid_id': bid.id,
                    'lock_id': lock_id,
                    'current_price': bid_amount
                }
                
            except Exception as e:
                # 보상: 재화 잠금 해제
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
            logger.error(f"Unexpected error: {e}", exc_info=True)
            
            # 보상 트랜잭션
            if lock_id:
                CurrencyLockService.release_currency_lock(
                    user_id,
                    auction_id,
                    lock_id
                )
            
            return {
                'success': False,
                'error': 'Internal server error',
                'step': 'unexpected'
            }


# tasks.py (Celery 비동기 작업)
from celery import shared_task
import logging

logger = logging.getLogger(__name__)

@shared_task(
    bind=True,
    max_retries=5,
    default_retry_delay=60
)
def retry_release_lock(self, user_id, auction_id, lock_id):
    """
    재화 잠금 해제 재시도
    
    실패 시 exponential backoff으로 재시도
    """
    try:
        success = CurrencyLockService.release_currency_lock(
            user_id,
            auction_id,
            lock_id,
            retry=False  # 무한 재시도 방지
        )
        
        if not success:
            raise Exception("Release lock failed")
        
        logger.info(f"Lock released successfully via retry: {lock_id}")
        
    except Exception as e:
        logger.error(f"Retry release lock failed: {e}")
        
        # Exponential backoff
        retry_delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=e, countdown=retry_delay)


@shared_task
def release_previous_lock(user_id, auction_id, lock_id):
    """이전 입찰자 잠금 해제"""
    try:
        CurrencyLockService.release_currency_lock(
            user_id,
            auction_id,
            lock_id
        )
    except Exception as e:
        logger.error(f"Failed to release previous lock: {e}")


@shared_task
def send_bid_notifications(auction_id, user_id, amount):
    """입찰 알림 발송"""
    # WebSocket, 이메일, 푸시 알림 등
    pass


@shared_task
def cleanup_expired_locks():
    """
    만료된 잠금 정리
    
    Celery Beat로 주기적 실행
    """
    from .models import CurrencyLock, UserCurrency
    from django.utils import timezone
    from datetime import timedelta
    
    # 5분 이상 된 locked 상태 조회
    expired_time = timezone.now() - timedelta(minutes=5)
    expired_locks = CurrencyLock.objects.filter(
        status='locked',
        locked_at__lt=expired_time
    )
    
    cleaned_count = 0
    
    for lock in expired_locks:
        try:
            with transaction.atomic():
                user_currency = UserCurrency.objects.select_for_update().get(
                    user_id=lock.user_id
                )
                
                user_currency.locked_amount -= lock.amount
                if user_currency.locked_amount < 0:
                    user_currency.locked_amount = 0
                
                user_currency.save()
                
                lock.status = 'expired'
                lock.save()
                
                cleaned_count += 1
                
                logger.warning(
                    f"Cleaned expired lock: user={lock.user_id}, "
                    f"auction={lock.auction_id}, amount={lock.amount}"
                )
                
        except Exception as e:
            logger.error(f"Failed to clean lock {lock.id}: {e}")
    
    logger.info(f"Cleaned {cleaned_count} expired locks")
    return cleaned_count
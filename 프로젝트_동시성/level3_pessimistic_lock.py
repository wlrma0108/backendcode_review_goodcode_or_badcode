# Level 3: select_for_update (비관적 락)
# 개선: DB row 잠금으로 동시성 제어

from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
import logging

logger = logging.getLogger(__name__)

class BidCreateView(APIView):
    def post(self, request, auction_id):
        user = request.user
        amount = request.data.get('amount')
        
        try:
            with transaction.atomic():
                # 1. Currency row 잠금 (다른 트랜잭션 대기)
                currency = Currency.objects.select_for_update().get(user=user)
                
                if currency.available_balance < amount:
                    raise ValueError('Insufficient balance')
                
                # 2. Auction row 잠금
                auction = Auction.objects.select_for_update().get(id=auction_id)
                
                if auction.status != 'active':
                    raise ValueError('Auction is not active')
                
                if amount <= auction.current_price:
                    raise ValueError(f'Bid must be higher than {auction.current_price}')
                
                # 3. 이전 입찰자 재화 해제
                if auction.current_winner:
                    previous_winner_currency = Currency.objects.select_for_update().get(
                        user=auction.current_winner
                    )
                    previous_winner_currency.balance += auction.current_price
                    previous_winner_currency.locked_balance -= auction.current_price
                    previous_winner_currency.save()
                
                # 4. 현재 입찰자 재화 잠금
                currency.balance -= amount
                currency.locked_balance += amount
                currency.save()
                
                # 5. 입찰 생성
                bid = Bid.objects.create(
                    auction=auction,
                    user=user,
                    amount=amount
                )
                
                # 6. 경매 업데이트
                auction.current_price = amount
                auction.current_winner = user
                auction.save()
                
                logger.info(f"Bid successful: user={user.id}, auction={auction_id}, amount={amount}")
                
                return Response({'bid_id': bid.id})
                
        except ValueError as e:
            logger.warning(f"Bid failed: {e}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            return Response({'error': 'Internal error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


"""
개선점:
✓ select_for_update로 row 잠금
✓ 동시 접근 시 순차 처리
✓ Race condition 해결
✓ 이전 입찰자 재화 자동 해제

동작 원리:
Thread 1: currency.select_for_update() → row 잠금 획득
Thread 2: currency.select_for_update() → 대기...
Thread 1: 입찰 처리 완료 → 커밋 → 잠금 해제
Thread 2: 잠금 획득 → 처리 시작

문제점:

1. 성능 저하:
   - 동시 입찰 시 대기 발생
   - 인기 경매는 큐잉됨
   - 처리 시간 증가

2. 데드락 가능성:
   Thread 1: currency A 잠금 → auction 1 잠금 대기
   Thread 2: auction 1 잠금 → currency A 잠금 대기
   → 서로 대기 → 데드락

3. 타임아웃 문제:
   - 락 대기가 너무 길면 타임아웃
   - 사용자는 에러 받음
   - 재시도 필요

4. 확장성:
   - DB 커넥션 점유
   - 여러 경매 동시 입찰 시 병목

개선 필요:
- 락 순서 일관성 (항상 currency → auction)
- 타임아웃 설정
- 성능 모니터링
"""

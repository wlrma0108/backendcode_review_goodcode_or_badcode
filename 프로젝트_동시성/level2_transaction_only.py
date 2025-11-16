# Level 2: DB 트랜잭션 추가
# 개선: 원자성 보장, 하지만 여전히 race condition

from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

class BidCreateView(APIView):
    def post(self, request, auction_id):
        user = request.user
        amount = request.data.get('amount')
        
        try:
            # 트랜잭션으로 묶음 - 전부 성공 or 전부 실패
            with transaction.atomic():
                # 1. 재화 확인 및 차감
                currency = Currency.objects.get(user=user)
                
                if currency.available_balance < amount:
                    raise ValueError('Insufficient balance')
                
                currency.balance -= amount
                currency.locked_balance += amount
                currency.save()
                
                # 2. 경매 확인
                auction = Auction.objects.get(id=auction_id)
                
                if amount <= auction.current_price:
                    # 트랜잭션 롤백됨 - 재화 복구 OK
                    raise ValueError(f'Bid must be higher than {auction.current_price}')
                
                # 3. 입찰 생성
                bid = Bid.objects.create(
                    auction=auction,
                    user=user,
                    amount=amount
                )
                
                # 4. 경매 업데이트
                auction.current_price = amount
                auction.current_winner = user
                auction.save()
                
                return Response({'bid_id': bid.id})
                
        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {'error': 'Internal error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


"""
개선점:
✓ 입찰 실패 시 재화 자동 복구 (롤백)
✓ 데이터 일관성 보장 (all or nothing)

여전한 문제:

시나리오 1: 재화 동시 차감
User A (balance=10000):
  Thread 1: 트랜잭션 시작
    currency 조회 (10000)
    5000 차감 계산
  Thread 2: 트랜잭션 시작
    currency 조회 (10000) ← 아직 Thread 1 커밋 전!
    6000 차감 계산
  Thread 1: save() → balance=5000 커밋
  Thread 2: save() → balance=4000 커밋 (덮어씀)
  
  결과: Lost Update

시나리오 2: 동시 입찰
  Thread 1: current_price=0 확인 → 1000 입찰 OK
  Thread 2: current_price=0 확인 → 1000 입찰 OK
  Thread 1: current_price=1000 저장
  Thread 2: current_price=1000 저장 (덮어씀)
  
  결과: 둘 다 성공

왜? 트랜잭션은 격리는 하지만 Lock이 없음
"""

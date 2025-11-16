# Level 1: 동시성 제어 없음
# 문제: Race Condition 발생

# models.py
from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class Currency(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    locked_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    @property
    def available_balance(self):
        return self.balance - self.locked_balance


class Auction(models.Model):
    title = models.CharField(max_length=200)
    current_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    current_winner = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20, default='active')


class Bid(models.Model):
    auction = models.ForeignKey(Auction, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)


# views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

class BidCreateView(APIView):
    def post(self, request, auction_id):
        user = request.user
        amount = request.data.get('amount')
        
        # 1. 재화 확인
        currency = Currency.objects.get(user=user)
        if currency.available_balance < amount:
            return Response(
                {'error': 'Insufficient balance'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # 2. 경매 확인
        auction = Auction.objects.get(id=auction_id)
        if amount <= auction.current_price:
            return Response(
                {'error': 'Bid too low'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # 3. 재화 차감
        currency.balance -= amount
        currency.locked_balance += amount
        currency.save()
        
        # 4. 입찰 생성
        bid = Bid.objects.create(
            auction=auction,
            user=user,
            amount=amount
        )
        
        # 5. 경매 업데이트
        auction.current_price = amount
        auction.current_winner = user
        auction.save()
        
        return Response({'bid_id': bid.id})


"""
문제점:

시나리오 1: 재화 이중 차감
User A (balance=10000):
  Thread 1: 조회 (10000) → 5000 입찰
  Thread 2: 조회 (10000) → 6000 입찰
  Thread 1: balance = 5000 저장
  Thread 2: balance = 4000 저장 (덮어씀)
  
  결과: 6000만 차감, 5000 입찰은 재화 안 잠김

시나리오 2: 동시 입찰 둘 다 성공
User A, B가 동시에 1000원 입찰:
  A: current_price=0 확인 → OK
  B: current_price=0 확인 → OK
  A: current_price=1000, winner=A 저장
  B: current_price=1000, winner=B 저장 (덮어씀)
  
  결과: 둘 다 성공, A는 재화 잠김, B가 winner
"""

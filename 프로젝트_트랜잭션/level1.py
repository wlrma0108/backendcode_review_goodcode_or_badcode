# models.py
from django.db import models
from django.contrib.auth.models import User

class UserCurrency(models.Model):
    """사용자 재화"""
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    amount = models.IntegerField(default=0)
    
    def __str__(self):
        return f"{self.user.username}: {self.amount}"


class Auction(models.Model):
    title = models.CharField(max_length=200)
    current_price = models.IntegerField(default=0)
    current_winner = models.ForeignKey(
        User, 
        null=True, 
        blank=True,
        on_delete=models.SET_NULL
    )
    status = models.CharField(max_length=20, default='active')
    
    def __str__(self):
        return self.title


class Bid(models.Model):
    auction = models.ForeignKey(Auction, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.IntegerField()
    timestamp = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-timestamp']


# views.py
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response

@api_view(['POST'])
def place_bid(request, auction_id):
    """
    가장 기본적인 입찰 처리
    
    문제점:
    1. 재화 확인 안 함
    2. 재화 차감 안 함
    3. 동시 입찰 시 race condition
    4. 입찰 실패 시 처리 없음
    """
    user = request.user
    bid_amount = request.data.get('amount')
    
    try:
        # 단순히 경매 조회
        auction = Auction.objects.get(id=auction_id)
        
        # 기본 검증
        if bid_amount <= auction.current_price:
            return Response({
                'success': False,
                'error': 'Bid too low'
            }, status=400)
        
        # 입찰 생성
        bid = Bid.objects.create(
            auction=auction,
            user=user,
            amount=bid_amount
        )
        
        # 경매 업데이트
        auction.current_price = bid_amount
        auction.current_winner = user
        auction.save()
        
        return Response({
            'success': True,
            'bid_id': bid.id
        })
        
    except Auction.DoesNotExist:
        return Response({
            'success': False,
            'error': 'Auction not found'
        }, status=404)

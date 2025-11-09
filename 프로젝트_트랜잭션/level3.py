
# models.py
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

class UserCurrency(models.Model):
    """사용자 재화"""
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    total_amount = models.IntegerField(default=0)  # 전체 재화
    locked_amount = models.IntegerField(default=0)  # 잠긴 재화
    
    @property
    def available_amount(self):
        """사용 가능한 재화"""
        return self.total_amount - self.locked_amount
    
    def __str__(self):
        return f"{self.user.username}: {self.total_amount} (locked: {self.locked_amount})"


class CurrencyLock(models.Model):
    """재화 잠금 기록"""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    auction = models.ForeignKey('Auction', on_delete=models.CASCADE)
    amount = models.IntegerField()
    locked_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ('locked', 'Locked'),
            ('released', 'Released'),
            ('consumed', 'Consumed'),
        ],
        default='locked'
    )
    
    class Meta:
        indexes = [
            models.Index(fields=['user', 'auction', 'status']),
        ]


class Auction(models.Model):
    title = models.CharField(max_length=200)
    current_price = models.IntegerField(default=0)
    current_winner = models.ForeignKey(
        User, 
        null=True, 
        blank=True,
        on_delete=models.SET_NULL,
        related_name='winning_auctions'
    )
    status = models.CharField(max_length=20, default='active')
    
    def __str__(self):
        return self.title


class Bid(models.Model):
    auction = models.ForeignKey(Auction, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.IntegerField()
    timestamp = models.DateTimeField(auto_now_add=True)
    currency_lock = models.ForeignKey(
        CurrencyLock,
        null=True,
        on_delete=models.SET_NULL
    )
    is_winning = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['-timestamp']


# views.py
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import Auction, Bid, UserCurrency, CurrencyLock

@api_view(['POST'])
def place_bid_v3(request, auction_id):
   
    user = request.user
    bid_amount = request.data.get('amount')
    
    try:
        with transaction.atomic():
            # 1. 재화 조회 및 잠금 (row lock)
            user_currency = UserCurrency.objects.select_for_update().get(
                user=user
            )
            
            # 2. 사용 가능한 재화 확인
            if user_currency.available_amount < bid_amount:
                return Response({
                    'success': False,
                    'error': f'Insufficient currency. Available: {user_currency.available_amount}'
                }, status=400)
            
            # 3. 경매 조회 및 잠금
            auction = Auction.objects.select_for_update().get(
                id=auction_id
            )
            
            # 4. 입찰 금액 검증
            if bid_amount <= auction.current_price:
                return Response({
                    'success': False,
                    'error': f'Bid must be higher than {auction.current_price}'
                }, status=400)
            
            # 5. 이전 최고 입찰자의 재화 잠금 해제
            if auction.current_winner:
                previous_locks = CurrencyLock.objects.filter(
                    user=auction.current_winner,
                    auction=auction,
                    status='locked'
                )
                
                for lock in previous_locks:
                    # 이전 입찰자의 잠금 해제
                    previous_user_currency = UserCurrency.objects.select_for_update().get(
                        user=lock.user
                    )
                    previous_user_currency.locked_amount -= lock.amount
                    previous_user_currency.save()
                    
                    lock.status = 'released'
                    lock.save()
                    
                    # 이전 입찰 상태 업데이트
                    Bid.objects.filter(
                        user=lock.user,
                        auction=auction,
                        is_winning=True
                    ).update(is_winning=False)
            
            # 6. 현재 사용자의 재화 잠금
            user_currency.locked_amount += bid_amount
            user_currency.save()
            
            # 7. 잠금 기록 생성
            currency_lock = CurrencyLock.objects.create(
                user=user,
                auction=auction,
                amount=bid_amount,
                status='locked'
            )
            
            # 8. 입찰 생성
            bid = Bid.objects.create(
                auction=auction,
                user=user,
                amount=bid_amount,
                currency_lock=currency_lock,
                is_winning=True
            )
            
            # 9. 경매 업데이트
            auction.current_price = bid_amount
            auction.current_winner = user
            auction.save()
            
            return Response({
                'success': True,
                'bid_id': bid.id,
                'locked_amount': user_currency.locked_amount,
                'available_amount': user_currency.available_amount
            })
            
    except UserCurrency.DoesNotExist:
        return Response({
            'success': False,
            'error': 'User currency not found'
        }, status=404)
    except Auction.DoesNotExist:
        return Response({
            'success': False,
            'error': 'Auction not found'
        }, status=404)
    except Exception as e:
        return Response({
            'success': False,
            'error': str(e)
        }, status=500)


@api_view(['POST'])
def finalize_auction(request, auction_id):
    
    try:
        with transaction.atomic():
            auction = Auction.objects.select_for_update().get(id=auction_id)
            
            if auction.status != 'active':
                return Response({
                    'success': False,
                    'error': 'Auction already finalized'
                }, status=400)
            
            # 낙찰자 재화 차감
            if auction.current_winner:
                winner_currency = UserCurrency.objects.select_for_update().get(
                    user=auction.current_winner
                )
                
                # 잠금된 재화를 실제로 차감
                winner_lock = CurrencyLock.objects.get(
                    user=auction.current_winner,
                    auction=auction,
                    status='locked'
                )
                
                winner_currency.total_amount -= winner_lock.amount
                winner_currency.locked_amount -= winner_lock.amount
                winner_currency.save()
                
                winner_lock.status = 'consumed'
                winner_lock.save()
            
            # 다른 입찰자들의 잠금 해제 (이미 해제됐어야 하지만 안전장치)
            remaining_locks = CurrencyLock.objects.filter(
                auction=auction,
                status='locked'
            ).exclude(user=auction.current_winner)
            
            for lock in remaining_locks:
                user_currency = UserCurrency.objects.select_for_update().get(
                    user=lock.user
                )
                user_currency.locked_amount -= lock.amount
                user_currency.save()
                
                lock.status = 'released'
                lock.save()
            
            auction.status = 'ended'
            auction.save()
            
            return Response({
                'success': True,
                'winner': auction.current_winner.username if auction.current_winner else None,
                'final_price': auction.current_price
            })
            
    except Auction.DoesNotExist:
        return Response({
            'success': False,
            'error': 'Auction not found'
        }, status=404)
    except Exception as e:
        return Response({
            'success': False,
            'error': str(e)
        }, status=500)
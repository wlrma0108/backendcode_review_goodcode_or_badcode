
# views.py
from django.db import transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import Auction, Bid, UserCurrency

@api_view(['POST'])
def place_bid_v2(request, auction_id):

    user = request.user
    bid_amount = request.data.get('amount')
    
    try:
        # 1. 재화 확인
        user_currency = UserCurrency.objects.get(user=user)
        
        if user_currency.amount < bid_amount:
            return Response({
                'success': False,
                'error': f'Insufficient currency. You have {user_currency.amount}'
            }, status=400)
        
        # 2. 재화 차감
        user_currency.amount -= bid_amount
        user_currency.save()
        
        # 3. 경매 조회
        auction = Auction.objects.get(id=auction_id)
        
        # 4. 입찰 검증
        if bid_amount <= auction.current_price:
            # 문제: 재화는 이미 차감됨!
            return Response({
                'success': False,
                'error': 'Bid too low'
            }, status=400)
        
        # 5. 입찰 생성
        bid = Bid.objects.create(
            auction=auction,
            user=user,
            amount=bid_amount
        )
        
        # 6. 경매 업데이트
        auction.current_price = bid_amount
        auction.current_winner = user
        auction.save()
        
        return Response({
            'success': True,
            'bid_id': bid.id
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


@api_view(['POST'])
def place_bid_v2_with_transaction(request, auction_id):

    user = request.user
    bid_amount = request.data.get('amount')
    
    try:
        with transaction.atomic():
            # 1. 재화 확인 및 차감
            user_currency = UserCurrency.objects.get(user=user)
            
            if user_currency.amount < bid_amount:
                return Response({
                    'success': False,
                    'error': f'Insufficient currency'
                }, status=400)
            
            user_currency.amount -= bid_amount
            user_currency.save()
            
            # 2. 경매 조회 및 검증
            auction = Auction.objects.get(id=auction_id)
            
            if bid_amount <= auction.current_price:
                # 트랜잭션 롤백되어 재화 복구됨
                raise Exception('Bid too low')
            
            # 3. 입찰 생성
            bid = Bid.objects.create(
                auction=auction,
                user=user,
                amount=bid_amount
            )
            
            # 4. 경매 업데이트
            auction.current_price = bid_amount
            auction.current_winner = user
            auction.save()
            
            return Response({
                'success': True,
                'bid_id': bid.id
            })
            
    except Exception as e:
        return Response({
            'success': False,
            'error': str(e)
        }, status=400)
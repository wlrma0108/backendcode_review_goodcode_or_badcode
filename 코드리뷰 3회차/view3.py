# 중첩된 N+1 문제가 발생하는 뷰
def bad_category_view(request):
    # 1. 모든 카테고리를 가져오는 쿼리 1번 발생
    categories = Category.objects.all()

    # 2. 각 카테고리에 대해 루프
    for category in categories:
        print(f"카테고리: {category.name}")
        # 3. category.products.all() 접근 시, 상품 목록을 가져오는 쿼리 발생 (N번)
        for product in category.products.all():
            # 4. product.seller 접근 시, 판매자 정보를 가져오는 쿼리 또 발생 (M번, M은 총 상품 수)
            #    총 쿼리 수 = 1(카테고리) + N(상품목록) + M(판매자) -> 매우 비효율적
            print(f"  - 상품: {product.name}, 판매자: {product.seller.shop_name}")

    return render(request, 'categories.html', {'categories': categories})
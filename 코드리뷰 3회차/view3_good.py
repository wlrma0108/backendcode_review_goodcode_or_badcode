from django.db.models import Prefetch

# prefetch_related와 select_related를 함께 사용하여 문제를 해결한 뷰
def good_category_view(request):
    # 쿼리 최적화 과정:
    # 1. `prefetch_related('products')`
    #    - Category와 연결된 Product들을 가져오기 위해 별도의 쿼리를 준비합니다. (총 쿼리 2번)
    # 2. `select_related('seller')`
    #    - Prefetch 해오는 Product 쿼리셋 내에서 Seller 정보를 JOIN을 통해 미리 가져오도록 지정합니다.
    #    - 즉, Product를 가져오는 쿼리(1번)가 Seller 정보까지 포함하게 됩니다.
    # 결과적으로 단 2번의 쿼리로 카테고리, 상품, 판매자 정보를 모두 가져옵니다.
    categories = Category.objects.prefetch_related(
        Prefetch('products', queryset=Product.objects.select_related('seller'))
    ).all()

    # 3. 루프를 돌 때 DB 접근이 전혀 발생하지 않음
    for category in categories:
        print(f"카테고리: {category.name}")
        for product in category.products.all(): # 캐싱된 데이터 사용
            print(f"  - 상품: {product.name}, 판매자: {product.seller.shop_name}") # 캐싱된 데이터 사용

    return render(request, 'categories.html', {'categories': categories})
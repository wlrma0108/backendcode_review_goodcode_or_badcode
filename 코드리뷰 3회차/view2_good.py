# prefetch_related를 사용하여 N+1 문제를 해결한 뷰
def good_post_view(request):
    # 1. 모든 게시글을 가져오는 쿼리 1번 발생
    # 2. prefetch_related가 게시글에 연결된 모든 태그 정보를 별도의 쿼리(단 1번)로 미리 가져옴
    #    그 후, 파이썬 레벨에서 게시글과 태그를 조합해줌
    #    총 2번의 쿼리로 모든 정보를 가져올 수 있음
    posts = Post.objects.prefetch_related('tags').all()

    # 3. 루프를 돌 때 추가 쿼리가 발생하지 않음
    #    이미 posts 객체 안에 tags 정보가 캐싱되어 있기 때문
    for post in posts:
        tag_names = [tag.name for tag in post.tags.all()] # 이 접근에서 DB 쿼리가 발생하지 않음
        print(f"게시글: {post.title}, 태그: {', '.join(tag_names)}")

    return render(request, 'posts.html', {'posts': posts})
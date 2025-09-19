# N+1 문제가 발생하는 뷰
def bad_post_view(request):
    # 1. 모든 게시글을 가져오는 쿼리 1번 발생
    posts = Post.objects.all()

    # 2. 각 게시글에 대해 루프를 돌면서 tags에 접근할 때마다 추가 쿼리 발생
    #    게시글이 50개라면, 50번의 추가 쿼리가 발생하여 총 51번의 쿼리가 실행됨
    for post in posts:
        # 이 시점에서 post.tags.all()을 실행하면 해당 게시글의 태그를 찾기 위해 다시 DB에 쿼리
        tag_names = [tag.name for tag in post.tags.all()]
        print(f"게시글: {post.title}, 태그: {', '.join(tag_names)}")

    return render(request, 'posts.html', {'posts': posts})
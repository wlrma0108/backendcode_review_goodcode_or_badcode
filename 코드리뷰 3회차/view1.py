# N+1 문제가 발생하는 뷰
def bad_comment_view(request):
    # 1. 모든 댓글을 가져오는 쿼리 1번 발생
    comments = Comment.objects.all()

    # 2. 각 댓글에 대해 루프를 돌면서 user 정보에 접근할 때마다 추가 쿼리 발생
    #    만약 댓글이 100개라면, 100번의 추가 쿼리가 발생하여 총 101번의 쿼리가 실행됨
    for comment in comments:
        # 이 시점에서 comment.user에 접근하면 데이터베이스에 다시 쿼리를 보냄
        print(f"댓글: {comment.content}, 작성자: {comment.user.username}")

    return render(request, 'comments.html', {'comments': comments})
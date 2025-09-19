# select_related를 사용하여 N+1 문제를 해결한 뷰
def good_comment_view(request):
    # 1. 댓글을 가져올 때, 정방향 참조하는 User 모델을 JOIN하여 함께 가져옴
    #    SQL의 JOIN과 유사하게 동작하여, 단 한 번의 쿼리로 댓글과 사용자 정보를 모두 조회
    comments = Comment.objects.select_related('user').all()

    # 2. 루프를 돌 때 추가 쿼리가 발생하지 않음
    #    이미 comments 객체 안에 user 정보가 포함되어 있기 때문
    for comment in comments:
        print(f"댓글: {comment.content}, 작성자: {comment.user.username}")

    return render(request, 'comments.html', {'comments': comments})
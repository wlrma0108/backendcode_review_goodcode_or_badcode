from abc import ABC, abstractmethod

class Shape(ABC):  # 추상 클래스
    @abstractmethod
    def area(self):
        pass

class Rectangle(Shape):
    def __init__(self, w, h):
        self.w, self.h = w, h

    def area(self):
        return self.w * self.h

class Circle(Shape):
    def __init__(self, r):
        self.r = r

    def area(self):
        return 3.14 * self.r * self.r

shapes = [Rectangle(3, 4), Circle(5)]
for s in shapes:
    print(f"도형의 넓이: {s.area()}")

class Animal:
    def __init__(self, name):
        self.name = name

    def speak(self):
        return f"{self.name}가 소리를 냅니다."

class Cat(Animal):
    def speak(self):  # 메서드 오버라이딩
        return f"{self.name}가 야옹 합니다."

cat1 = Cat("나비")
print(cat1.speak())

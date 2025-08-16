class Dog:
    def __init__(self, name, breed):
        self.name = name
        self.breed = breed

    def bark(self):
        return f"{self.name}가 멍멍 짖습니다!"

dog1 = Dog("바둑이", "진돗개")
print(dog1.bark())

class BankAccount:
    def __init__(self, owner, balance=0):
        self.owner = owner
        self.__balance = balance  # private 속성

    def deposit(self, amount):
        self.__balance += amount

    def withdraw(self, amount):
        if amount <= self.__balance:
            self.__balance -= amount
        else:
            print("잔액 부족!")

    def get_balance(self):
        return self.__balance

account = BankAccount("홍길동", 1000)
account.deposit(500)
account.withdraw(200)
print(account.get_balance())

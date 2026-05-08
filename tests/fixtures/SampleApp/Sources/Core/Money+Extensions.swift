extension Money {
    public var isPositive: Bool { amount > 0 }

    public func doubled() -> Money {
        return Money(amount: amount * 2, currency: currency)
    }
}

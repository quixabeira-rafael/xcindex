public func +(lhs: Money, rhs: Money) -> Money {
    return Money(amount: lhs.amount + rhs.amount, currency: lhs.currency)
}

public prefix func -(value: Money) -> Money {
    return Money(amount: -value.amount, currency: value.currency)
}

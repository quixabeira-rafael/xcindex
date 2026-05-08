public struct Money {
    public let amount: Double
    public let currency: String

    public init(amount: Double, currency: String = "USD") {
        self.amount = amount
        self.currency = currency
    }

    public static func zero() -> Money {
        return Money(amount: 0)
    }

    public func formatted() -> String {
        return "\(currency) \(amount)"
    }
}

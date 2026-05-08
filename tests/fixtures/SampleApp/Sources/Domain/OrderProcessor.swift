import Core

public class OrderProcessor {
    private let calculator: PriceProvider

    public init(calculator: PriceProvider = PriceCalculator()) {
        self.calculator = calculator
    }

    public func charge() -> Money {
        let total = calculator.price()
        return Money(amount: total)
    }
}

public protocol PriceProvider {
    func price() -> Double
}

public class PriceCalculator: PriceProvider {
    public init() {}

    public func price() -> Double {
        return compute()
    }

    public func compute() -> Double {
        return 100.0
    }
}

public class DiscountedPriceCalculator: PriceCalculator {
    public override func compute() -> Double {
        return super.compute() * 0.8
    }
}

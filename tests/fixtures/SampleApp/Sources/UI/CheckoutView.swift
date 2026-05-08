import Domain
import Core

public struct CheckoutView {
    public let processor: OrderProcessor

    public init() {
        self.processor = OrderProcessor()
    }

    public func render() -> String {
        let money = processor.charge()
        return "Total: \(money.formatted())"
    }
}

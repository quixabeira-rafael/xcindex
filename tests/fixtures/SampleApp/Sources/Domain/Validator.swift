public protocol Validator {
    associatedtype Input
    func validate(_ input: Input) -> Bool
}

extension Validator {
    public func validateAll(_ inputs: [Input]) -> Bool {
        return inputs.allSatisfy { validate($0) }
    }
}

public struct PositiveAmountValidator: Validator {
    public typealias Input = Double

    public init() {}

    public func validate(_ input: Double) -> Bool {
        return input > 0
    }
}

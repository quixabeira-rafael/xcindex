import Core

public class Receipt {
    public var total: Double = 0 {
        didSet {
            print("total changed from \(oldValue) to \(total)")
        }
    }

    private var _items: [String] = []

    public var items: [String] {
        get { _items }
        set {
            _items = newValue
            total = Double(newValue.count) * 10.0
        }
    }

    public static let identifier = "receipt"

    public init() {}

    deinit {
        print("Receipt deallocated")
    }

    public func record(name: String) {
        var current = items
        current.append(name)
        items = current
    }
}

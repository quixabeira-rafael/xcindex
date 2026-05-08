public typealias MoneyBox = Container<Money>

public struct Container<Element> {
    public private(set) var items: [Element] = []

    public init() {}

    public mutating func add(_ item: Element) {
        items.append(item)
    }

    public subscript(index: Int) -> Element {
        return items[index]
    }
}

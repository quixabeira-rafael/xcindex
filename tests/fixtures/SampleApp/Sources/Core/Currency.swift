public enum Currency: String, CaseIterable {
    case usd = "USD"
    case eur = "EUR"
    case gbp = "GBP"

    public var symbol: String {
        switch self {
        case .usd: return "$"
        case .eur: return "€"
        case .gbp: return "£"
        }
    }

    public static var defaultCurrency: Currency { .usd }
}

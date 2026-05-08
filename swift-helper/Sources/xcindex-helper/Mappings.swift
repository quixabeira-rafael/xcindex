// IndexStore type → string mappings written into the cache.
//
// The Python query layer matches on these exact strings for `kind`,
// `sub_kind`, `language`, and relation `kind`. Any change here is a
// breaking schema change — bump `Schema.version`.

import Foundation
import IndexStore

enum Mappings {

    static func kindString(_ kind: IndexStoreSymbol.Kind) -> String {
        switch kind {
        case .unknown:           return "unknown"
        case .module:            return "module"
        case .namespace:         return "namespace"
        case .namespaceAlias:    return "namespace-alias"
        case .macro:             return "macro"
        case .enum:              return "enum"
        case .struct:            return "struct"
        case .class:             return "class"
        case .protocol:          return "protocol"
        case .extension:         return "extension"
        case .union:             return "union"
        case .typealias:         return "typealias"
        case .function:          return "function"
        case .variable:          return "variable"
        case .field:             return "field"
        case .enumConstant:      return "enum-case"
        case .instanceMethod:    return "instance-method"
        case .classMethod:       return "class-method"
        case .staticMethod:      return "static-method"
        case .instanceProperty:  return "instance-property"
        case .classProperty:     return "class-property"
        case .staticProperty:    return "static-property"
        case .constructor:       return "constructor"
        case .destructor:        return "destructor"
        case .conversionFunction: return "conversion-function"
        case .parameter:         return "parameter"
        case .using:             return "using"
        case .commentTag:        return "comment-tag"
        default:                 return "unknown"
        }
    }

    /// Returns nil when subkind is `.none` (never written to SQLite).
    static func subKindString(_ subKind: IndexStoreSymbol.SubKind) -> String? {
        switch subKind {
        case .none:                          return nil
        case .cxxCopyConstructor:            return "cxx-copy-constructor"
        case .cxxMoveConstructor:            return "cxx-move-constructor"
        case .accessorGetter:                return "accessor-getter"
        case .accessorSetter:                return "accessor-setter"
        case .swiftAccessorWillSet:          return "swift-accessor-willset"
        case .swiftAccessorDidSet:           return "swift-accessor-didset"
        case .swiftAccessorAddressor:        return "swift-accessor-addressor"
        case .swiftAccessorMutableAddressor: return "swift-accessor-mutable-addressor"
        case .swiftExtensionOfStruct:        return "swift-extension-of-struct"
        case .swiftExtensionOfClass:         return "swift-extension-of-class"
        case .swiftExtensionOfEnum:          return "swift-extension-of-enum"
        case .swiftExtensionOfProtocol:      return "swift-extension-of-protocol"
        case .swiftPrefixOperator:           return "swift-prefix-operator"
        case .swiftPostfixOperator:          return "swift-postfix-operator"
        case .swiftInfixOperator:            return "swift-infix-operator"
        case .swiftSubscript:                return "swift-subscript"
        case .swiftAssociatedtype:           return "swift-associated-type"
        case .swiftGenericTypeParam:         return "swift-generic-type-param"
        default:                             return nil
        }
    }

    static func languageString(_ language: IndexStoreLanguage) -> String {
        switch language {
        case .c:           return "c"
        case .cxx:         return "cxx"
        case .objectiveC:  return "objc"
        case .swift:       return "swift"
        default:           return "unknown"
        }
    }

    /// Pick the most informative role bit to represent a relation's primary kind.
    /// Order matters — the first matching bit wins.
    static func primaryRelationKind(_ roles: IndexStoreSymbolRoles) -> String {
        if roles.contains(.childOf)            { return "childOf" }
        if roles.contains(.baseOf)             { return "baseOf" }
        if roles.contains(.overrideOf)         { return "overrideOf" }
        if roles.contains(.receivedBy)         { return "receivedBy" }
        if roles.contains(.calledBy)           { return "calledBy" }
        if roles.contains(.extendedBy)         { return "extendedBy" }
        if roles.contains(.accessorOf)         { return "accessorOf" }
        if roles.contains(.containedBy)        { return "containedBy" }
        if roles.contains(.ibTypeOf)           { return "ibTypeOf" }
        if roles.contains(.specializationOf)   { return "specializationOf" }
        return "other"
    }
}

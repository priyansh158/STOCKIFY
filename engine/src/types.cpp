#include "stocksim/types.hpp"

namespace stocksim {

namespace {

// Linear search over the enum's values: the sets are tiny and this keeps one source of truth (to_string).
template <typename E, std::size_t N>
std::optional<E> parse_from(std::string_view s, const E (&values)[N]) {
    for (E v : values) {
        if (to_string(v) == s) {
            return v;
        }
    }
    return std::nullopt;
}

}  // namespace

std::optional<Side> parse_side(std::string_view s) {
    static constexpr Side all[] = {Side::Buy, Side::Sell};
    return parse_from(s, all);
}

std::optional<Product> parse_product(std::string_view s) {
    static constexpr Product all[] = {Product::CNC, Product::MIS, Product::NRML};
    return parse_from(s, all);
}

std::optional<OrderType> parse_order_type(std::string_view s) {
    static constexpr OrderType all[] = {OrderType::Market, OrderType::Limit, OrderType::SL, OrderType::SLM};
    return parse_from(s, all);
}

std::optional<Validity> parse_validity(std::string_view s) {
    static constexpr Validity all[] = {Validity::Day, Validity::IOC};
    return parse_from(s, all);
}

std::optional<OrderStatus> parse_order_status(std::string_view s) {
    static constexpr OrderStatus all[] = {OrderStatus::Pending,  OrderStatus::Open,      OrderStatus::TriggerPending,
                                          OrderStatus::Complete, OrderStatus::Cancelled, OrderStatus::Rejected,
                                          OrderStatus::Sliced};
    return parse_from(s, all);
}

std::optional<Reason> parse_reason(std::string_view s) {
    static constexpr Reason all[] = {Reason::None,     Reason::Instrument, Reason::Product, Reason::Session,
                                     Reason::Cas,      Reason::QtyLot,     Reason::Tick,    Reason::Band,
                                     Reason::Freeze,   Reason::Holdings,   Reason::NoReference, Reason::Margin,
                                     Reason::Eod,      Reason::Ioc,        Reason::User,    Reason::Rms};
    return parse_from(s, all);
}

}  // namespace stocksim

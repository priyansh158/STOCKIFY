#include "stocksim/ledger.hpp"

#include <algorithm>

namespace stocksim {

const Position* Ledger::position(InstrumentId iid, Product product) const {
    const auto it = index_.find({iid, product});
    if (it == index_.end()) {
        return nullptr;
    }
    const Position& p = positions_[it->second];
    return p.q != 0 ? &p : nullptr;
}

Position& Ledger::get_or_open(InstrumentId iid, Product product, CashStyle style) {
    const auto it = index_.find({iid, product});
    if (it != index_.end()) {
        return positions_[it->second];
    }
    index_[{iid, product}] = positions_.size();
    Position p;
    p.instrument = iid;
    p.product = product;
    p.style = style;
    positions_.push_back(std::move(p));
    return positions_.back();
}

Paise Ledger::apply_fill(InstrumentId iid, Product product, CashStyle style, Side side, Qty qty, Paise price,
                         std::optional<Ppb> cnc_credit_ppb, std::optional<Date> trade_date) {  // ledger.apply_fill
    Position& p = get_or_open(iid, product, style);
    const int sign = side == Side::Buy ? 1 : -1;
    const Paise realised_before = p.realised;
    Qty remaining = qty;
    if (p.q != 0 && p.d != sign) {  // reducing (and maybe flipping)
        const Qty k = std::min(remaining, p.q);
        const Paise removed = k == p.q ? p.c : mul_div_floor(p.c, k, p.q);
        p.realised = checked_add(p.realised, p.signed_by_side(checked_sub(checked_mul(k, price), removed)));
        p.c -= removed;
        p.q -= k;
        remaining -= k;
    }
    if (remaining != 0) {
        if (p.q == 0) {
            p.d = sign;
            p.c = 0;
        }
        p.q += remaining;
        p.c = checked_add(p.c, checked_mul(remaining, price));
    }
    if (style == CashStyle::Premium) {
        const Paise value = checked_mul(qty, price);
        cash = checked_sub(cash, sign * value);
        if (side == Side::Sell && product == Product::CNC && cnc_credit_ppb && trade_date) {
            const Paise held_back = value - mul_div_floor(value, *cnc_credit_ppb, kPpb);
            unavailable[*trade_date] += held_back;
        }
    } else if (p.q == 0) {  // a variation position closed: settle what is left
        settle_variation(p, price, "fill");
    }
    return p.realised - realised_before;
}

Paise Ledger::settle_variation(Position& p, Paise price, const std::string& source) {  // ledger.settle_variation
    const Paise delta = checked_sub(p.pnl(price), p.vm);
    cash = checked_add(cash, delta);
    p.vm = checked_add(p.vm, delta);
    p.last_mark = price;
    p.mark_source = source;
    return delta;
}

void Ledger::debit(Paise amount) {
    cash = checked_sub(cash, amount);
    charges_total = checked_add(charges_total, amount);
}

void Ledger::release_day(Date trade_date) {
    for (auto it = unavailable.begin(); it != unavailable.end();) {
        it = it->first < trade_date ? unavailable.erase(it) : std::next(it);
    }
}

}  // namespace stocksim

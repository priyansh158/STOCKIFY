// Cash, positions and NAV (A4 §6), the twin of stockify.oracle.ledger. Integers only.
//
// Positions are keyed by (instrument, product) and kept in insertion order, because the oracle iterates its
// position dict in that order when it settles expiring contracts (log lines, charge rows and settlement
// closes come out in that order). Two cash styles:
//   premium   (CNC equities, options): the traded value moves at each fill; NAV counts the position at its mark;
//   variation (futures, MIS equities): nothing moves at the fill; cash moves by pnl(m) − vm when the position is
//             marked and when it goes flat, where pnl(x) = realised + d·(q·x − c).
#pragma once

#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "stocksim/int128.hpp"
#include "stocksim/types.hpp"

namespace stocksim {

enum class CashStyle : std::uint8_t { Premium, Variation };

struct Position {
    InstrumentId instrument = 0;
    Product product = Product::CNC;
    CashStyle style = CashStyle::Premium;
    Qty q = 0;              // open units, ≥ 0
    int d = 1;              // +1 long, −1 short
    Paise c = 0;            // exact cost of the open units (Σ price × units)
    Paise realised = 0;
    Paise vm = 0;           // variation already moved to cash
    std::optional<Paise> last_mark;
    std::string mark_source;

    Qty qty() const { return d * q; }
    // d is +1 or -1: the sign flip of a value, where INT64_MIN has no negation (overflow is an error).
    Paise signed_by_side(Paise v) const { return d > 0 ? v : checked_neg(v); }
    Paise avg_price() const { return q != 0 ? floor_div(c, q) : 0; }
    Paise pnl(Paise price) const { return checked_add(realised, signed_by_side(checked_sub(checked_mul(q, price), c))); }
};

class Ledger {
 public:
    Ledger(Paise cash, Paise capital) : cash(cash), capital(capital) {}

    Paise cash;
    Paise capital;
    Paise charges_total = 0;
    Paise reserved = 0;
    std::map<Date, Paise> unavailable;  // trade date → CNC sale proceeds not yet usable

    const Position* position(InstrumentId iid, Product product) const;  // an open position, else nullptr
    Position& get_or_open(InstrumentId iid, Product product, CashStyle style);
    std::vector<Position>& positions() { return positions_; }  // insertion order, closed ones included
    const std::vector<Position>& positions() const { return positions_; }

    // Books one fill; returns the realised P&L it produced.
    Paise apply_fill(InstrumentId iid, Product product, CashStyle style, Side side, Qty qty, Paise price,
                     std::optional<Ppb> cnc_credit_ppb, std::optional<Date> trade_date);
    Paise settle_variation(Position& p, Paise price, const std::string& source);
    void debit(Paise amount);
    void release_day(Date trade_date);

    // mark(iid) → price or nullopt (then the position's last mark, else its average price).
    template <typename Mark>
    Paise nav(const Mark& mark) const {
        Paise total = cash;
        for (const Position& p : positions_) {
            if (p.q == 0) {
                continue;
            }
            std::optional<Paise> m = mark(p.instrument);
            if (!m) {
                m = p.last_mark ? *p.last_mark : p.avg_price();
            }
            total = p.style == CashStyle::Premium ? checked_add(total, checked_mul(p.qty(), *m))
                                                  : checked_add(total, checked_sub(p.pnl(*m), p.vm));
        }
        return total;
    }

 private:
    std::vector<Position> positions_;
    std::map<std::pair<InstrumentId, Product>, std::size_t> index_;
};

}  // namespace stocksim

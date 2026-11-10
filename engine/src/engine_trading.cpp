// Orders (A4 §3), fills (§4), completion and charges (§5), funds, settlement and the end of day (§6, §7).
// Each function names the stockify.oracle.runner method it mirrors.
#include <algorithm>
#include <stdexcept>

#include "stocksim/engine.hpp"
#include "stocksim/errors.hpp"
#include "stocksim/int128.hpp"
#include "stocksim/matcher.hpp"

namespace stocksim {

namespace {

constexpr std::string_view kSystemTags[] = {"AUTO_SQOFF", "RMS_SQOFF"};

bool is_system_tag(std::string_view tag) {
    return std::find(std::begin(kSystemTags), std::end(kSystemTags), tag) != std::end(kSystemTags);
}

std::string iso(Date d) { return iso_date(d); }

// The A4 §3.2 check order: the spelled codes are checked against A4 by blockwatch, and the static_assert
// below proves the enum order spells exactly them.
// <block name="reject-codes" same-as="docs/plan/A4_engine_semantics.md:reject-codes" same-as-mode="sequence" same-as-pattern='"(?P<value>R_[A-Z_]+)"'>
constexpr std::string_view kCheckOrderCodes[] = {"R_INSTRUMENT", "R_PRODUCT", "R_SESSION", "R_CAS", "R_QTY_LOT", "R_TICK", "R_BAND", "R_FREEZE",
                                                 "R_HOLDINGS", "R_NO_REFERENCE", "R_MARGIN"};
// </block>
constexpr Reason kCheckOrder[] = {Reason::Instrument, Reason::Product, Reason::Session, Reason::Cas,
                                  Reason::QtyLot, Reason::Tick, Reason::Band, Reason::Freeze,
                                  Reason::Holdings, Reason::NoReference, Reason::Margin};

constexpr bool check_order_spelled() {
    if (std::size(kCheckOrder) != std::size(kCheckOrderCodes)) {
        return false;
    }
    for (std::size_t i = 0; i < std::size(kCheckOrder); ++i) {
        if (to_string(kCheckOrder[i]) != kCheckOrderCodes[i]) {
            return false;
        }
    }
    return true;
}
static_assert(check_order_spelled(), "kCheckOrder must spell the A4 §3.2 codes in order");

}  // namespace

const Spec& Engine::spec_or_throw(InstrumentId iid, Date d) const {
    const Spec* s = spec(iid, d);
    if (s == nullptr) {
        throw RunError("instrument " + std::to_string(iid) + " has no spec on " + iso(d));
    }
    return *s;
}

// ── Funds (A4 §6.5) ──────────────────────────────────────────────────────────

std::optional<Paise> Engine::mark_price(InstrumentId iid) const {  // runner.mark_price
    const auto it = market_.find(iid);
    if (it == market_.end()) {
        return std::nullopt;
    }
    const MarketState& m = it->second;
    if (mark_kind_ == "mid" && m.bid && *m.bid != 0 && m.ask && *m.ask != 0) {
        return floor_div(checked_add(*m.bid, *m.ask), 2);
    }
    return m.ltp;
}

std::optional<Paise> Engine::ref_price(InstrumentId iid) const {  // runner._ref_price: mark or the last price seen
    const std::optional<Paise> m = mark_price(iid);
    if (m && *m != 0) {
        return m;
    }
    const auto it = underlying_last_.find(iid);
    return it == underlying_last_.end() ? std::nullopt : std::optional<Paise>(it->second);
}

Paise Engine::blocked(const Ledger& ledger, Date d) const {  // runner.blocked
    std::vector<MarginLeg> legs;
    Paise mis = 0;
    const std::string date = iso(d);
    for (const Position& p : ledger.positions()) {
        if (p.q == 0) {
            continue;
        }
        if (p.product == Product::MIS) {
            const std::optional<Paise> m = mark_price(p.instrument);
            mis = checked_add(mis, margin_->mis(instruments_.at(p.instrument).kind, p.q,
                                               m && *m != 0 ? *m : p.avg_price(), date));
        } else {
            legs.push_back(MarginLeg{p.instrument, p.product, p.qty()});
        }
    }
    return checked_add(margin_->requirement(legs, instruments_, [this](InstrumentId i) { return ref_price(i); }, date),
                       mis);
}

Funds Engine::funds(const Ledger& ledger) const {  // runner.funds
    const Paise b = blocked(ledger, date_of(now_));
    Paise held = 0;
    for (const auto& [d, v] : ledger.unavailable) {
        held = checked_add(held, v);
    }
    return Funds{ledger.cash - b - ledger.reserved - held, ledger.cash, b, ledger.reserved};
}

bool Engine::reduces(InstrumentId iid, Product product, Side side, Qty qty) const {  // runner._reduces
    const Position* p = book_.ledger.position(iid, product);
    return p != nullptr && p->d != (side == Side::Buy ? 1 : -1) && qty <= p->q;
}

Paise Engine::required(const Order& o, Paise px) const {  // runner._required
    const Instrument& row = instruments_.at(o.instrument);
    const std::string date = iso(date_of(now_));
    if (reduces(o.instrument, o.product, o.side, o.qty)) {
        return 0;
    }
    if (o.product == Product::CNC) {
        return o.side == Side::Buy ? checked_mul(o.qty, px) : 0;
    }
    if (o.product == Product::MIS) {
        return margin_->mis(row.kind, o.qty, px, date);
    }
    if (row.is_option() && o.side == Side::Buy) {
        return checked_mul(o.qty, px);
    }
    std::vector<MarginLeg> legs;
    for (const Position& p : book_.ledger.positions()) {
        if (p.q != 0 && p.product == Product::NRML) {
            legs.push_back(MarginLeg{p.instrument, p.product, p.qty()});
        }
    }
    // after_legs = legs + [(o, NRML, signed qty)], combined per (instrument, product) in first-seen order
    std::vector<MarginLeg> combined;
    auto add = [&](const MarginLeg& leg) {
        for (MarginLeg& c : combined) {
            if (c.instrument == leg.instrument && c.product == leg.product) {
                c.units += leg.units;
                return;
            }
        }
        combined.push_back(leg);
    };
    for (const MarginLeg& leg : legs) {
        add(leg);
    }
    add(MarginLeg{o.instrument, Product::NRML, o.qty * o.sign()});
    // before and after (the staircase prices each underlying at one step for both, MEZ 4.9)
    const auto [before, after] = margin_->pair(
        legs, combined, instruments_, [this](InstrumentId i) { return ref_price(i); },
        [&](InstrumentId i) { return i == o.instrument ? std::optional<Paise>(px) : ref_price(i); }, date);
    return std::max<Paise>(0, after - before);
}

// ── Orders (A4 §3) ───────────────────────────────────────────────────────────

Bp Engine::bp(const Order& o) const {  // runner._bp
    if (o.protection) {
        return *o.protection;
    }
    const Json& policy = require(bundle_.root, "broker_policy", "bundle");
    const auto it = policy.find("market_protection_auto_bp");
    const std::string date = iso(date_of(now_));
    const Json* row = it == policy.end() ? nullptr : row_in_force(*it, date);
    if (row == nullptr) {
        throw RunError("no market_protection_auto_bp row on " + date);
    }
    return row->at("bp").get<Bp>();
}

void Engine::event(Book& book, const Order& o, std::string_view ev, std::string detail) {  // runner._event
    book.order_events.push_back(OrderEventRow{now_, o.order_id, std::string(ev), std::move(detail)});
    // Every event the oracle records (SUBMIT … SLICE) queues an on_order_update.
    Callback cb;
    cb.order_id = o.order_id;
    book.callbacks.push_back(std::move(cb));
}

Order& Engine::reject(Order& o, Reason code) {  // runner._reject
    o.status = OrderStatus::Rejected;
    o.reason = code;
    book_.ledger.reserved -= o.reserved;
    o.reserved = 0;
    ++rejects_[std::string(to_string(code))];
    event(book_, o, "REJECT", std::string(to_string(code)));
    log("INFO", "order " + std::to_string(o.order_id) + " rejected " + std::string(to_string(code)));
    return o;
}

OrderId Engine::place(const PlaceRequest& r, OrderId parent, bool system) {  // runner.place
    Order o;
    o.order_id = next_order_++;
    o.parent_id = parent;
    o.instrument = r.instrument;
    o.side_text = r.side;
    o.product_text = r.product;
    o.type_text = r.type;
    o.validity_text = r.validity;
    o.side = parse_side(r.side).value_or(Side::Buy);
    o.product = parse_product(r.product).value_or(Product::CNC);
    o.type = parse_order_type(r.type).value_or(OrderType::Market);
    o.validity = parse_validity(r.validity).value_or(Validity::Day);
    o.qty = r.qty;
    o.price = r.price;
    o.trigger = r.trigger;
    o.protection = r.protection;
    o.tag = r.tag;
    o.t_submit = now_;
    const OrderId id = o.order_id;
    Order& ord = book_.orders.emplace(id, std::move(o)).first->second;
    event(book_, ord, "SUBMIT", r.side + " " + std::to_string(r.qty) + " " + r.product + " " + r.type);
    if (const auto code = validate(ord, system, Reason::Instrument, Reason::Freeze)) {
        return reject(ord, *code).order_id;
    }
    const Spec& sp = spec_or_throw(r.instrument, date_of(now_));
    if (sp.freeze_qty && *sp.freeze_qty != 0 && r.qty > *sp.freeze_qty) {
        if (policies_.at("freeze").get<std::string>() == "reject" && !system) {
            return reject(ord, Reason::Freeze).order_id;
        }
        const Qty child = floor_to(*sp.freeze_qty, sp.lot);
        const Qty n = ceil_div(r.qty, child);  // a zero child is a ZeroDivisionError in the oracle too
        if (child <= 0 || n > policies_.at("max_slices").get<std::int64_t>()) {
            return reject(ord, Reason::Freeze).order_id;
        }
        ord.status = OrderStatus::Sliced;
        event(book_, ord, "SLICE", std::to_string(n) + " children");
        Qty left = r.qty;
        while (left != 0) {
            const Qty q = std::min(child, left);
            left -= q;
            PlaceRequest c = r;
            c.qty = q;
            place(c, id, system);
        }
        return id;
    }
    Order& again = book_.orders.at(id);  // the map is stable, but read it fresh for clarity
    if (const auto code = validate(again, system, Reason::Holdings, Reason::Margin)) {
        return reject(again, *code).order_id;
    }
    accept(again);
    return id;
}

std::optional<Reason> Engine::validate(Order& o, bool system, Reason start, Reason upto) {  // runner._validate
    const Instrument* row = instruments_.find(o.instrument);
    const Date d = date_of(now_);
    bool on = false;
    for (const Reason code : kCheckOrder) {
        if (code == start) {
            on = true;
        }
        if (!on) {
            continue;
        }
        switch (code) {
            case Reason::Instrument:
                if (row == nullptr || row->kind == "INDEX" || row->kind == "FUT_CONT" ||
                    (row->last_trading_ns && now_ >= *row->last_trading_ns) || !parse_side(o.side_text) ||
                    !parse_order_type(o.type_text) || !parse_validity(o.validity_text)) {
                    return code;
                }
                break;
            case Reason::Product: {
                const bool deriv = row->is_derivative();
                if (!parse_product(o.product_text) || (deriv && o.product == Product::CNC) ||
                    (!deriv && o.product == Product::NRML)) {
                    return code;
                }
                break;
            }
            case Reason::Session: {
                if (system) {
                    break;
                }
                const std::string_view g = group_of(o.instrument, d);
                const DaySchedule* day = calendar_.day(d);
                const std::optional<TimeNs> cutoff = day != nullptr ? day->cutoff(cutoff_key(g)) : std::nullopt;
                if (o.product == Product::MIS && cutoff && now_ >= *cutoff) {
                    return code;
                }
                if (policies_.at("preopen").get<std::string>() == "reject" && phase_at(g, now_) != "CONTINUOUS") {
                    return code;
                }
                break;
            }
            case Reason::Cas: {
                if (system) {
                    break;
                }
                const std::string_view g = group_of(o.instrument, d);
                if (g == "CM_CAS" && phase_at(g, now_) == "CAS" && policies_.at("cas").get<std::string>() == "none") {
                    return code;
                }
                break;
            }
            case Reason::QtyLot: {
                const Spec* sp = spec(o.instrument, d);
                if (sp == nullptr || o.qty <= 0) {
                    return code;
                }
                if (o.qty % sp->lot != 0) {
                    bool any_lot = false;
                    for (const Spec& s : row->specs) {
                        if (s.from <= d && o.qty % s.lot == 0) {
                            any_lot = true;
                        }
                    }
                    if (!(reduces(o.instrument, o.product, o.side, o.qty) && any_lot)) {
                        return code;
                    }
                }
                break;
            }
            case Reason::Tick: {
                const Paise tick = spec_or_throw(o.instrument, d).tick;
                if ((o.type == OrderType::Limit || o.type == OrderType::SL) && (o.price <= 0 || o.price % tick != 0)) {
                    return code;
                }
                if ((o.type == OrderType::SL || o.type == OrderType::SLM) &&
                    (o.trigger <= 0 || o.trigger % tick != 0)) {
                    return code;
                }
                break;
            }
            case Reason::Band: {
                const auto m = market_.find(o.instrument);
                if (m != market_.end() && m->second.band &&
                    (o.type == OrderType::Limit || o.type == OrderType::SL) &&
                    !(m->second.band->first <= o.price && o.price <= m->second.band->second)) {
                    return code;
                }
                break;
            }
            case Reason::Freeze:
                break;  // handled by the caller (slice / reject)
            case Reason::Holdings:
                if (o.product == Product::CNC && o.side == Side::Sell) {
                    const Position* p = book_.ledger.position(o.instrument, Product::CNC);
                    const Qty held = p != nullptr && p->d > 0 ? p->qty() : 0;
                    Qty open_sells = 0;
                    for (const auto& [xid, x] : book_.orders) {
                        if (xid != o.order_id && x.instrument == o.instrument && x.product == Product::CNC &&
                            x.side == Side::Sell && x.live() && parse_product(x.product_text) &&
                            parse_side(x.side_text)) {
                            open_sells += x.remaining();
                        }
                    }
                    if (o.qty + open_sells > held) {
                        return code;
                    }
                }
                break;
            case Reason::NoReference:
                if (o.type == OrderType::Market && !mark_price(o.instrument)) {  // SL-M uses its trigger
                    return code;
                }
                break;
            case Reason::Margin:
                if (!system) {
                    const Paise need = required(o, order_px(o));
                    // A4 §6.5: only risk-increasing orders are refused, so a shortfall can be traded out of
                    if (need > funds(book_.ledger).available && !reduces(o.instrument, o.product, o.side, o.qty)) {
                        return code;
                    }
                    o.reserved = need;
                    book_.ledger.reserved += need;
                }
                break;
            default:
                break;
        }
        if (code == upto) {
            break;
        }
    }
    return std::nullopt;
}

Paise Engine::order_px(const Order& o) const {  // runner._order_px
    const Paise tick = spec_or_throw(o.instrument, date_of(now_)).tick;
    if (o.type == OrderType::Limit || o.type == OrderType::SL) {
        return o.price;
    }
    const Paise ref = o.type == OrderType::SLM ? o.trigger : mark_price(o.instrument).value();
    return protection_limit(o.side, ref, bp(o), tick);
}

void Engine::accept(Order& o) {  // runner._accept
    const std::string g(group_of(o.instrument));
    o.t_activate = o.t_submit + latency_ns_;
    if (phase_at(g, o.t_activate) != "CONTINUOUS") {  // queued for the next session, next_days included (A4 §3.10)
        const std::optional<TimeNs> nxt = cal_all_.next_continuous_start(
            [&](Date d) { return instruments_.group_of(o.instrument, d); }, o.t_submit);
        if (!nxt) {
            reject(o, Reason::Session);
            return;
        }
        o.t_activate = std::max(*nxt + latency_ns_, o.t_activate);
    }
    o.status = OrderStatus::Pending;
    o.trade_date = date_of(o.t_activate);
}

std::vector<OrderId> Engine::live_orders() {  // runner.live_orders
    const auto final_or_absent = [this](OrderId id) {
        const auto it = book_.orders.find(id);
        return it == book_.orders.end() || it->second.final();  // ids absent after a sod_state load were final
    };
    while (min_live_ < next_order_ && final_or_absent(min_live_)) {
        ++min_live_;
    }
    std::vector<OrderId> out;
    for (OrderId i = min_live_; i < next_order_; ++i) {
        if (!final_or_absent(i)) {
            out.push_back(i);
        }
    }
    return out;
}

void Engine::activate_due(TimeNs t) {  // runner._activate_due
    std::vector<OrderId> due;
    for (const OrderId id : live_orders()) {
        const Order& o = book_.orders.at(id);
        if (o.status == OrderStatus::Pending && o.t_activate <= t) {
            due.push_back(id);
        }
    }
    std::stable_sort(due.begin(), due.end(), [&](OrderId a, OrderId b) {
        const Order& x = book_.orders.at(a);
        const Order& y = book_.orders.at(b);
        return x.t_activate != y.t_activate ? x.t_activate < y.t_activate : a < b;
    });
    for (const OrderId id : due) {
        activate(book_.orders.at(id));
    }
}

void Engine::activate(Order& o) {  // runner._activate
    const Paise tick = spec_or_throw(o.instrument, date_of(now_)).tick;
    if (o.type == OrderType::Market) {
        const std::optional<Paise> ref = mark_price(o.instrument);
        if (!ref) {
            reject(o, Reason::NoReference);
            return;
        }
        o.protection_bp = bp(o);
        o.limit = protection_limit(o.side, *ref, *o.protection_bp, tick);
        o.from_market = true;
        o.status = OrderStatus::Open;
    } else if (o.type == OrderType::Limit) {
        o.limit = o.price;
        o.status = OrderStatus::Open;
    } else {
        if (o.type == OrderType::SLM) {
            o.protection_bp = bp(o);
        }
        o.status = OrderStatus::TriggerPending;
    }
    event(book_, o, "ACTIVATE", o.limit ? "limit " + std::to_string(*o.limit) : "");
}

bool Engine::modify(OrderId id, const ModifyRequest& m) {  // runner.modify
    const auto it = book_.orders.find(id);
    if (it == book_.orders.end() || !it->second.live()) {
        return false;
    }
    Order& o = it->second;
    if (m.qty && *m.qty < o.filled_qty) {
        log("INFO", "modify " + std::to_string(id) + " refused: qty below filled");
        return false;
    }
    const std::string new_type = m.type.value_or(o.type_text);
    static const std::pair<std::string_view, std::string_view> allowed[] = {
        {"LIMIT", "MARKET"}, {"SL", "LIMIT"}, {"SL", "MARKET"}, {"SL-M", "LIMIT"}, {"SL-M", "MARKET"}};
    if (new_type != o.type_text &&
        std::find(std::begin(allowed), std::end(allowed), std::pair<std::string_view, std::string_view>(o.type_text, new_type)) ==
            std::end(allowed)) {
        log("INFO", "modify " + std::to_string(id) + " refused: " + o.type_text + " -> " + new_type);
        return false;
    }
    if (m.qty) {
        o.qty = *m.qty;
    }
    if (m.price) {
        o.price = *m.price;
    }
    if (m.trigger) {
        o.trigger = *m.trigger;
    }
    if (new_type != o.type_text) {
        o.type_text = new_type;
        o.type = parse_order_type(new_type).value_or(OrderType::Market);
        o.status = OrderStatus::Pending;
    }
    o.t_activate = std::max(o.t_activate, now_ + latency_ns_);
    o.status = OrderStatus::Pending;
    o.from_market = false;
    o.limit.reset();
    event(book_, o, "MODIFY", m.detail);
    return true;
}

bool Engine::cancel(OrderId id, Reason reason) {  // runner.cancel
    const auto it = book_.orders.find(id);
    if (it == book_.orders.end() || !it->second.live()) {
        return false;
    }
    cancel_in(book_, it->second, reason);
    return true;
}

void Engine::cancel_in(Book& book, Order& o, Reason reason) {  // runner._cancel
    o.status = OrderStatus::Cancelled;
    o.reason = reason;
    book.ledger.reserved -= o.reserved;
    o.reserved = 0;
    event(book, o, "CANCEL", std::string(to_string(reason)));
    if (o.filled_qty != 0) {
        charge_order(book, o);
    }
}

OrderView Engine::view_of(const Order& o) const {  // orders.OrderView.of
    return OrderView{o.order_id, o.instrument, o.side_text, o.qty, o.product_text, o.type_text, o.price, o.trigger,
                     std::string(to_string(o.status)), std::string(to_string(o.reason)), o.filled_qty,
                     o.avg_price(), o.tag};
}

// ── Fills (A4 §4) and completion (§5.4) ──────────────────────────────────────

CashStyle Engine::style(InstrumentId iid, Product product) const {  // runner._style
    const Instrument& row = instruments_.at(iid);
    if (row.is_option()) {
        return CashStyle::Premium;
    }
    if (row.kind == "FUT" || product == Product::MIS) {
        return CashStyle::Variation;
    }
    return CashStyle::Premium;
}

std::optional<Ppb> Engine::cnc_credit_ppb() const {  // runner._cnc_credit_ppb
    const Json& policy = require(bundle_.root, "broker_policy", "bundle");
    const auto it = policy.find("cnc_sell_credit_same_day");
    if (it == policy.end()) {
        return std::nullopt;
    }
    const Json* row = row_in_force(*it, iso(date_of(now_)));
    if (row == nullptr || !row->contains("fraction_ppb") || row->at("fraction_ppb").is_null()) {
        return std::nullopt;
    }
    return row->at("fraction_ppb").get<Ppb>();
}

void Engine::fill(Book& book, Order& o, Qty qty, Paise price, const std::string& evidence, TimeNs t) {  // runner._fill
    const Date d = date_of(t);
    // The checked arithmetic first: an overflow leaves no half-recorded fill behind (M3 review).
    const Paise fill_value = checked_add(o.fill_value, checked_mul(qty, price));
    const Qty filled_qty = checked_add(o.filled_qty, qty);
    Fill f{book.next_fill++, o.order_id, t, o.instrument, o.side, qty, price, evidence};
    book.fills.push_back(f);
    o.filled_qty = filled_qty;
    o.fill_value = fill_value;
    o.trade_date = d;
    if (o.reserved != 0) {  // release the reserve pro rata (A4 §6.5)
        const Paise keep = mul_div_floor(o.reserved, o.remaining(), o.remaining() + qty);
        book.ledger.reserved -= o.reserved - keep;
        o.reserved = keep;
    }
    book.ledger.apply_fill(o.instrument, o.product, style(o.instrument, o.product), o.side, qty, price,
                           cnc_credit_ppb(), d);
    event(book, o, "FILL", std::to_string(qty) + "@" + std::to_string(price) + " " + evidence);
    Callback cb;
    cb.is_fill = true;
    cb.fill = f;
    book.callbacks.push_back(std::move(cb));
    if (o.remaining() == 0) {
        o.status = OrderStatus::Complete;
        book.ledger.reserved -= o.reserved;
        o.reserved = 0;
        event(book, o, "COMPLETE");
        charge_order(book, o);
    }
}

void Engine::charge_order(Book& book, Order& o) {  // runner._charge_order
    if (o.charged || o.filled_qty == 0) {
        return;
    }
    o.charged = true;
    const std::string& kind = instruments_.at(o.instrument).kind;
    const Date td = *o.trade_date;
    charge_row(book, "order", o.order_id, o.instrument, td, costs_.order(kind, o.product, o.side, o.fill_value, iso(td)));
    if (o.product == Product::CNC && o.side == Side::Sell && book.dp_done.count({o.instrument, td}) == 0) {
        if (const auto dp = costs_.dp(iso(td))) {
            book.dp_done.insert({o.instrument, td});
            charge_row(book, "dp", 0, o.instrument, td, *dp);
        }
    }
    if (is_system_tag(o.tag)) {
        if (const auto fee = costs_.sqoff_fee(iso(td))) {
            charge_row(book, "sqoff_fee", o.order_id, o.instrument, td, *fee);
        }
    }
}

void Engine::charge_row(Book& book, const std::string& kind, OrderId order_id, InstrumentId iid, Date d,
                        const ChargeParts& parts) {  // runner._charge_row
    const Paise tot = parts.row_total();
    book.charges.push_back(ChargeRow{kind, order_id, iid, d, parts, tot});
    book.ledger.debit(tot);
}

std::vector<OrderId> Engine::eligible(InstrumentId iid, TimeNs t_start) {  // runner._eligible
    std::vector<OrderId> out;
    for (const OrderId id : live_orders()) {
        const Order& o = book_.orders.at(id);
        if (o.instrument == iid && o.active() && o.t_activate <= t_start) {
            out.push_back(id);
        }
    }
    std::stable_sort(out.begin(), out.end(), [&](OrderId a, OrderId b) {
        const Order& x = book_.orders.at(a);
        const Order& y = book_.orders.at(b);
        return x.t_activate != y.t_activate ? x.t_activate < y.t_activate : a < b;
    });
    return out;
}

bool Engine::band_ok(InstrumentId iid, Paise price) const {  // runner._band_ok
    const auto it = market_.find(iid);
    if (it == market_.end() || !it->second.band) {
        return true;
    }
    return it->second.band->first <= price && price <= it->second.band->second;
}

// bar model --------------------------------------------------------------------

void Engine::simulate_bar(Book& book, InstrumentId iid, const BarFields& bar, TimeNs t, bool high_first,
                          const std::vector<OrderId>& eligible_ids, Paise tick, Qty lot) {  // runner._simulate_bar
    const PathPoints pts = path_points(bar.open, bar.high, bar.low, bar.close, high_first);
    const bool touch = fill_cfg_.at("limit_touch_policy").get<std::string>() == "touch";
    const Json& slip_cfg = fill_cfg_.at("slippage");
    struct Opp {
        PathPos pos;
        TimeNs t_activate;
        OrderId id;
        Opportunity opp;
    };
    std::vector<Opp> opps;
    for (const OrderId oid : eligible_ids) {
        const Order& o = book.orders.at(oid);
        auto protect = [&](Side side, Paise ref) {
            return protection_limit(side, ref, o.protection_bp ? *o.protection_bp : bp(o), tick);
        };
        if (auto opp = bar_opportunity(o, pts, touch, protect)) {
            opps.push_back(Opp{opp->pos, o.t_activate, oid, std::move(*opp)});
        }
    }
    std::stable_sort(opps.begin(), opps.end(), [](const Opp& a, const Opp& b) {
        if (a.pos != b.pos) {
            return a.pos < b.pos;
        }
        return a.t_activate != b.t_activate ? a.t_activate < b.t_activate : a.id < b.id;
    });
    Qty cap_left = participation_cap(bar.volume, fill_cfg_.at("participation_ppm").get<Ppm>(), lot);
    for (const Opp& x : opps) {
        Order& o = book.orders.at(x.id);
        const Opportunity& opp = x.opp;
        if (opp.trigger) {
            o.triggered = true;
            o.status = OrderStatus::Open;
            o.limit = opp.new_limit;
            o.from_market = opp.becomes_market;
            event(book, o, "TRIGGER", "at " + std::to_string(opp.price ? *opp.price : o.trigger));
        }
        if (!opp.price) {
            continue;
        }
        const Paise price = opp.slip ? slipped(o.side, *opp.price, tick, slip_cfg, book.rng, opp.cap) : *opp.price;
        if (opp.cap && (o.side == Side::Buy ? price > *opp.cap : price < *opp.cap)) {
            continue;
        }
        if (!band_ok(iid, price)) {
            continue;
        }
        const Qty qty = std::min(o.remaining(), cap_left);
        if (qty <= 0) {
            continue;
        }
        cap_left -= qty;
        fill(book, o, qty, price, opp.evidence, t);
    }
    for (const OrderId oid : eligible_ids) {
        book.orders.at(oid).first_eligible_done = true;
    }
}

void Engine::match_bar(InstrumentId iid, const BarFields& bar, TimeNs t) {  // runner._match_bar
    const Date d = date_of(bar.start_ns);
    const std::optional<TimeNs> cs = continuous_start(group_of(iid, d), d);  // the bar's own session (prev_days too)
    const TimeNs eff_start = cs ? std::max(bar.start_ns, *cs) : bar.start_ns;
    const std::vector<OrderId> ids = eligible(iid, eff_start);
    if (ids.empty()) {
        return;
    }
    const Spec& sp = spec_or_throw(iid, d);
    auto mark = [&](InstrumentId i) { return i == iid ? std::optional<Paise>(bar.close) : mark_price(i); };
    Book hl = book_.fork(iid);
    simulate_bar(hl, iid, bar, t, true, ids, sp.tick, sp.lot);  // P_HL
    Book lh = book_.fork(iid);
    simulate_bar(lh, iid, bar, t, false, ids, sp.tick, sp.lot);  // P_LH
    Book& chosen = hl.ledger.nav(mark) < lh.ledger.nav(mark) ? hl : lh;  // ties → P_LH
    Qty filled = 0;
    for (const Fill& f : chosen.fills) {
        filled += f.qty;
    }
    if (bar.volume != 0 && filled != 0) {
        participation_used_ = std::max(participation_used_, floor_div(checked_mul(filled, 1'000'000), bar.volume));
    }
    adopt(chosen);
}

void Engine::adopt(Book& sim) {  // runner._adopt
    book_.ledger = std::move(sim.ledger);
    for (auto& [id, o] : sim.orders) {
        book_.orders.insert_or_assign(id, std::move(o));
    }
    book_.rng = sim.rng;
    book_.fills.insert(book_.fills.end(), sim.fills.begin(), sim.fills.end());
    book_.charges.insert(book_.charges.end(), sim.charges.begin(), sim.charges.end());
    book_.order_events.insert(book_.order_events.end(), sim.order_events.begin(), sim.order_events.end());
    book_.callbacks.insert(book_.callbacks.end(), sim.callbacks.begin(), sim.callbacks.end());
    book_.dp_done = std::move(sim.dp_done);
    book_.next_fill = sim.next_fill;
}

// eod_close (A4 §4.9) -----------------------------------------------------------

void Engine::match_eod(InstrumentId iid, const BarFields& bar, TimeNs t) {  // runner._match_eod
    const Date d = date_of(bar.start_ns);
    const std::optional<TimeNs> cs = continuous_start(group_of(iid, d), d);  // the bar's own session (prev_days too)
    const TimeNs eff_start = cs ? std::max(bar.start_ns, *cs) : bar.start_ns;
    const std::vector<OrderId> ids = eligible(iid, eff_start);
    if (ids.empty()) {
        return;  // also every index bar: indices are not tradable
    }
    const Spec& sp = spec_or_throw(iid, d);
    const Paise tick = sp.tick;
    const std::int64_t h = fill_cfg_.at("half_spread_ticks").get<std::int64_t>();
    Qty cap_left = participation_cap(bar.volume, fill_cfg_.at("participation_ppm").get<Ppm>(), sp.lot);
    const Paise c = bar.close;
    for (const OrderId id : ids) {
        Order& o = book_.orders.at(id);
        const bool buy = o.side == Side::Buy;
        if (o.status == OrderStatus::TriggerPending) {
            if (buy ? c >= o.trigger : c <= o.trigger) {
                o.status = OrderStatus::Open;
                o.triggered = true;
                if (o.type == OrderType::SL) {
                    o.limit = o.price;
                } else {
                    o.limit = protection_limit(o.side, c, o.protection_bp && *o.protection_bp != 0 ? *o.protection_bp : bp(o), tick);
                }
                o.from_market = o.type == OrderType::SLM;
                event(book_, o, "TRIGGER", "at close " + std::to_string(c));
            } else {
                continue;
            }
        }
        Paise price = buy ? c + h * tick : c - h * tick;
        if (o.limit) {
            if (buy ? c > *o.limit : c < *o.limit) {
                o.first_eligible_done = true;
                continue;
            }
            price = buy ? std::min(price, *o.limit) : std::max(price, *o.limit);
        }
        const Qty qty = std::min(o.remaining(), cap_left);
        if (qty <= 0 || !band_ok(iid, price)) {
            continue;
        }
        cap_left -= qty;
        fill(book_, o, qty, price, "eod_close", t);
        o.first_eligible_done = true;
    }
    // The fill is the close of d, published at 19:00, after an expiring contract's 16:00 FSP: settle it now at
    // that FSP rather than falling back at DAY_END.
    const Instrument& row = instruments_.at(iid);
    if (row.is_derivative() && row.last_day() == d && row.underlying_id) {
        const auto fsp = fsp_seen_.find({*row.underlying_id, d});
        if (fsp != fsp_seen_.end()) {
            settle_final(*row.underlying_id, d, fsp->second, "settle");
        }
    }
}

// snapshot models (A4 §4.2, §4.3) ----------------------------------------------

void Engine::match_snapshot(InstrumentId iid, MarketState& m) {  // runner._match_snapshot
    const Date d = date_of(now_);
    const Spec& sp = spec_or_throw(iid, d);
    const Paise tick = sp.tick;
    const Qty lot = sp.lot;
    const TimeNs stale_ns = stale_after_ns_;
    const std::optional<Evidence> ev = evidence(m);
    const bool touch = fill_cfg_.at("limit_touch_policy").get<std::string>() == "touch";
    Qty cap_left = ev ? participation_cap(ev->dv, fill_cfg_.at("participation_ppm").get<Ppm>(), lot) : 0;
    std::optional<std::array<DepthLevel, 10>> working = m.book;  // levels consumed by this event's fills
    const TimeNs t1 = m.s1->t;
    for (const OrderId id : eligible(iid, t1)) {  // already in (t_activate, order_id) order
        Order& o = book_.orders.at(id);
        const bool buy = o.side == Side::Buy;
        const bool active_at_s0 = ev && o.t_activate <= ev->t0;
        if (o.status == OrderStatus::TriggerPending) {
            const bool hit = active_at_s0 ? (buy ? ev->hi >= o.trigger : ev->lo <= o.trigger)
                                          : (buy ? *m.ltp >= o.trigger : *m.ltp <= o.trigger);
            if (!hit) {
                continue;
            }
            o.status = OrderStatus::Open;
            o.triggered = true;
            if (o.type == OrderType::SLM) {
                const Paise ref = (buy ? *m.ltp <= o.trigger : *m.ltp >= o.trigger) ? o.trigger : *m.ltp;
                o.limit = protection_limit(o.side, ref, o.protection_bp && *o.protection_bp != 0 ? *o.protection_bp : bp(o), tick);
                o.from_market = true;
            } else {
                o.limit = o.price;
            }
            event(book_, o, "TRIGGER", "ltp " + std::to_string(*m.ltp));
        }
        if (!o.limit || !(o.t_activate < t1)) {
            continue;
        }
        const Paise lim = *o.limit;
        if (fill_kind_ == "snapshot_ltp") {
            const bool marketable = o.from_market || (buy ? *m.ltp <= lim : *m.ltp >= lim);
            if (marketable && ev && ev->dv > 0 && active_at_s0) {
                Paise base = 0;
                std::string evidence_name;
                if (ev->value) {
                    base = buy ? ceil_to(ev->value->ceil_div(ev->dv).to_int64(), tick)
                               : floor_to(ev->value->floor_div(ev->dv).to_int64(), tick);
                    evidence_name = "vwap_int";
                } else {
                    base = *m.ltp;
                    evidence_name = "ltp";
                }
                const Paise price = slipped(o.side, base, tick, fill_cfg_.at("slippage"), book_.rng, lim);
                if (buy ? price <= lim : price >= lim) {
                    const Qty qty = std::min(o.remaining(), cap_left);
                    if (qty > 0 && band_ok(iid, price)) {
                        cap_left -= qty;
                        fill(book_, o, qty, price, evidence_name, now_);
                    }
                }
            } else if (!marketable && ev && active_at_s0) {
                const bool through = buy ? (touch ? ev->lo <= lim : ev->lo < lim) : (touch ? ev->hi >= lim : ev->hi > lim);
                if (through) {
                    const Qty qty = std::min(o.remaining(), cap_left);
                    if (qty > 0) {
                        cap_left -= qty;
                        fill(book_, o, qty, lim, "trade_through", now_);
                    }
                }
            }
        } else {  // snapshot_quote
            if (!working || now_ - t1 > stale_ns) {
                continue;
            }
            const std::size_t first = buy ? 5 : 0;
            const Ppm depth_ppm = fill_cfg_.at("depth_take_ppm").get<Ppm>();
            if (!o.first_eligible_done) {
                for (std::size_t k = 0; k < 5; ++k) {
                    DepthLevel& level = (*working)[first + k];
                    if (level.price <= 0 || level.qty <= 0) {
                        continue;
                    }
                    if (buy ? level.price > lim : level.price < lim) {
                        break;
                    }
                    const Qty take = std::min(o.remaining(), floor_to(mul_div_floor(level.qty, depth_ppm, 1'000'000), lot));
                    if (take <= 0) {
                        continue;
                    }
                    level.qty -= take;
                    fill(book_, o, take, level.price, "book_lvl_" + std::to_string(k + 1), now_);
                    if (o.remaining() == 0) {
                        break;
                    }
                }
            } else {
                Qty qa = 0;
                for (std::size_t k = 0; k < 5; ++k) {
                    const DepthLevel& level = (*working)[first + k];
                    if (level.price > 0 && (buy ? level.price <= lim : level.price >= lim)) {
                        qa += level.qty;
                    }
                }
                qa = floor_to(mul_div_floor(qa, depth_ppm, 1'000'000), lot);
                Qty qb = 0;
                if (ev && active_at_s0 && (buy ? ev->lo < lim : ev->hi > lim)) {
                    qb = cap_left;
                }
                const Qty q = std::min(o.remaining(), std::max(qa, qb));
                if (q > 0) {
                    const std::string evidence_name = qa >= qb ? "book_cross" : "trade_through";
                    if (evidence_name == "trade_through") {
                        cap_left -= q;
                    }
                    fill(book_, o, q, lim, evidence_name, now_);
                }
            }
        }
        o.first_eligible_done = true;
    }
}

void Engine::after_market(InstrumentId iid) {  // runner._after_market (A4 §3.7)
    for (const OrderId id : live_orders()) {
        Order& o = book_.orders.at(id);
        if (o.instrument == iid && o.validity == Validity::IOC && o.active() && o.first_eligible_done) {
            cancel_in(book_, o, Reason::Ioc);
        }
    }
}

// ── Settlement (A4 §7.2) and the day (§1.10) ─────────────────────────────────

std::vector<std::size_t> Engine::expiring(InstrumentId underlying_id, Date d) {  // runner._expiring
    std::vector<std::size_t> out;
    const auto& ps = book_.ledger.positions();
    for (std::size_t i = 0; i < ps.size(); ++i) {
        const Instrument& row = instruments_.at(ps[i].instrument);
        if (ps[i].q != 0 && row.is_derivative() && row.underlying_id == std::optional<InstrumentId>(underlying_id) &&
            row.last_day() == d) {
            out.push_back(i);
        }
    }
    return out;
}

void Engine::settle_final(InstrumentId underlying_id, Date d, Paise fsp, const std::string& source) {  // runner._settle_final
    const std::vector<std::size_t> all = expiring(underlying_id, d);
    std::vector<std::size_t> physical;
    for (const std::size_t i : all) {
        if (instruments_.at(book_.ledger.positions()[i].instrument).settlement == std::optional<std::string>("physical")) {
            physical.push_back(i);
        }
    }
    if (!physical.empty()) {
        settle_physical(underlying_id, d, fsp, source, physical);
    }
    for (const std::size_t i : all) {
        Position& p = book_.ledger.positions()[i];
        const Instrument& row = instruments_.at(p.instrument);
        if (row.settlement == std::optional<std::string>("physical")) {
            continue;
        }
        if (row.settlement != std::optional<std::string>("cash")) {
            throw RunError(row.tradingsymbol + ": unknown settlement '" + row.settlement.value_or("") + "'");
        }
        Ledger& led = book_.ledger;
        const Side closing = p.d > 0 ? Side::Sell : Side::Buy;
        const Qty units = p.q;
        if (row.kind == "FUT") {
            settle_closes_.push_back(SettleClose{now_, p.instrument, p.product, closing, units, fsp});
            led.settle_variation(p, fsp, source);
            p.realised = p.pnl(fsp);
            p.vm = p.realised;
            p.q = 0;
            p.c = 0;
        } else {
            const Paise k = row.strike.value_or(0);
            const Paise intrinsic = row.kind == "CE" ? std::max<Paise>(0, fsp - k) : std::max<Paise>(0, k - fsp);
            settle_closes_.push_back(SettleClose{now_, p.instrument, p.product, closing, units, intrinsic});
            led.cash = checked_add(led.cash, checked_mul(p.qty(), intrinsic));
            p.realised = checked_add(p.realised, p.d * checked_sub(checked_mul(p.q, intrinsic), p.c));
            // D-59: brokerage on every option open at expiry; exercise STT only on a long ITM one's intrinsic value
            const Paise exercised = p.d > 0 ? checked_mul(p.q, intrinsic) : 0;
            charge_row(book_, "settlement", 0, p.instrument, d, costs_.exercise(row.kind, exercised, iso(d)));
            p.q = 0;
            p.c = 0;
            p.last_mark = intrinsic;
            p.mark_source = source;
        }
        log("INFO", "settled " + std::to_string(p.instrument) + " " + std::string(to_string(p.product)) + " at FSP " +
                        std::to_string(fsp) + " (" + source + ")");
    }
}

void Engine::settle_physical(InstrumentId stock, Date d, Paise fsp, const std::string& source,
                             std::vector<std::size_t> idx) {  // runner._settle_physical (A4 §7.3, D-58)
    const Instrument* srow = instruments_.find(stock);
    if (srow == nullptr || srow->kind != "EQ") {
        throw RunError(instruments_.at(book_.ledger.positions()[idx.front()].instrument).tradingsymbol +
                       ": physical settlement needs its underlying stock in the bundle");
    }
    Ledger& led = book_.ledger;
    std::sort(idx.begin(), idx.end(), [&](std::size_t a, std::size_t b) {
        const Position& x = led.positions()[a];
        const Position& y = led.positions()[b];
        return std::pair(x.instrument, to_string(x.product)) < std::pair(y.instrument, to_string(y.product));
    });
    struct Leg {
        bool take;
        Qty units;
        Paise price;
        InstrumentId contract;
        std::string kind;
        Paise exercised;
    };
    std::vector<Leg> legs;
    for (const std::size_t i : idx) {  // close every position first: booking the stock may grow the ledger
        Position& p = led.positions()[i];
        const Instrument& row = instruments_.at(p.instrument);
        const Side closing = p.d > 0 ? Side::Sell : Side::Buy;
        const Qty units = p.q;
        if (row.kind == "FUT") {
            settle_closes_.push_back(SettleClose{now_, p.instrument, p.product, closing, units, fsp});
            led.settle_variation(p, fsp, source);
            p.realised = p.pnl(fsp);
            p.vm = p.realised;
            legs.push_back(Leg{p.d > 0, units, fsp, p.instrument, row.kind, 0});
        } else {
            const Paise k = row.strike.value_or(0);
            settle_closes_.push_back(SettleClose{now_, p.instrument, p.product, closing, units, 0});
            p.realised = checked_add(p.realised, p.d * checked_sub(0, p.c));  // closes at 0: the premium is its P&L
            p.last_mark = 0;
            p.mark_source = source;
            const bool itm = row.kind == "CE" ? fsp > k : fsp < k;
            if (itm) {
                const bool take = (row.kind == "CE") == (p.d > 0);
                const Paise exercised = p.d > 0 ? checked_mul(units, fsp > k ? fsp - k : k - fsp) : 0;
                legs.push_back(Leg{take, units, k, p.instrument, row.kind, exercised});
            } else {  // D-59: an expiring OTM option pays the expiry brokerage
                charge_row(book_, "settlement", 0, p.instrument, d, costs_.exercise(row.kind, 0, iso(d)));
            }
        }
        p.q = 0;
        p.c = 0;
        log("INFO", "settled " + std::to_string(p.instrument) + " " + std::string(to_string(p.product)) + " at FSP " +
                        std::to_string(fsp) + " (" + source + ")");
    }
    Qty taken = 0;
    Qty given = 0;
    for (const Leg& l : legs) {
        if (l.take) {
            taken = checked_add(taken, l.units);
        } else {
            given = checked_add(given, l.units);
        }
    }
    Qty left_take = std::min(taken, given);
    Qty left_give = left_take;
    for (const Leg& l : legs) {
        Qty& left = l.take ? left_take : left_give;
        const Qty n = std::min(l.units, left);
        left -= n;
        const Side side = l.take ? Side::Buy : Side::Sell;
        led.apply_fill(stock, Product::CNC, style(stock, Product::CNC), side, l.units, l.price, cnc_credit_ppb(), d);
        if (const Position* held = led.position(stock, Product::CNC); held != nullptr && held->qty() < 0) {
            labels_.insert("short_delivery");
        }
        const ChargeParts parts =
            costs_.delivery(l.kind, checked_mul(l.units, l.price), checked_mul(n, l.price), l.exercised, iso(d));
        charge_row(book_, "settlement", 0, l.contract, d, parts);
        log("INFO", "delivered " + std::to_string(stock) + " CNC " + std::string(to_string(side)) + " " +
                        std::to_string(l.units) + " at " + std::to_string(l.price) + " (" + source + ")");
    }
}

bool Engine::daily_bars(InstrumentId iid) const {  // runner._daily_bars
    const auto it = market_.find(iid);
    return it != market_.end() && it->second.min_interval && *it->second.min_interval >= 86'400;
}

std::optional<double> Engine::exposure() const {  // runner.exposure
    std::set<std::string> groups;
    for (const Instrument& r : instruments_.rows()) {
        if (r.kind != "INDEX") {
            groups.insert(r.session_group);
        }
    }
    if (groups.empty()) {
        groups.insert("CM");
    }
    std::int64_t total = 0, held = 0;
    for (const DaySchedule& day : calendar_.days()) {
        std::vector<std::pair<TimeNs, TimeNs>> spans;
        for (const std::string& g : groups) {
            for (const PhaseSpan& p : day.group(g)) {
                if (p.phase == "CONTINUOUS") {
                    spans.emplace_back(p.start_ns, p.end_ns);
                }
            }
        }
        std::sort(spans.begin(), spans.end());
        std::vector<std::pair<TimeNs, TimeNs>> merged;
        for (const auto& [a, b] : spans) {
            if (!merged.empty() && a <= merged.back().second) {
                merged.back().second = std::max(merged.back().second, b);
            } else {
                merged.emplace_back(a, b);
            }
        }
        for (const auto& [a, b] : merged) {
            total += b - a;
            bool state = false;
            TimeNs cursor = a;
            bool stopped = false;
            for (const auto& [t, flag] : open_changes_) {
                if (t <= a) {
                    state = flag;
                    continue;
                }
                if (t >= b) {
                    stopped = true;
                    break;
                }
                if (state) {
                    held += t - cursor;
                }
                cursor = t;
                state = flag;
            }
            (void)stopped;
            if (state) {
                held += b - cursor;
            }
        }
    }
    if (total == 0) {
        return std::nullopt;
    }
    return static_cast<double>(held) / static_cast<double>(total);
}

}  // namespace stocksim

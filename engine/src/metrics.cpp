#include "stocksim/metrics.hpp"

#include <algorithm>
#include <cmath>
#include <deque>
#include <map>
#include <tuple>

#include "stocksim/margin.hpp"

namespace stocksim {

namespace {

double rate_on(const Json& rows, const std::string& d) {  // metrics._rate_on
    const Json* best = row_in_force(rows, d);
    return best != nullptr ? static_cast<double>(best->at("rate_ppb").get<std::int64_t>()) / 1'000'000'000.0 : 0.0;
}

JValue opt(const std::optional<double>& v) { return v ? JValue(*v) : JValue(); }

// Python's true division of two ints: correctly rounded when both fit in 53 bits (always, for these counts).
double div(std::int64_t a, std::int64_t b) { return static_cast<double>(a) / static_cast<double>(b); }

}  // namespace

JValue compute_metrics(const Engine& e) {  // metrics.compute
    const Json& run = require(e.bundle().root, "run", "bundle");
    const std::int64_t capital = require_int(run, "capital_paise", "run");
    const Json& pricing = require(e.bundle().root, "pricing", "bundle");
    const Json risk_free = pricing.contains("risk_free") ? pricing.at("risk_free") : Json::array();
    const auto& equity = e.equity();

    std::vector<double> navs{static_cast<double>(capital)};
    for (const EquityRow& row : equity) {
        navs.push_back(static_cast<double>(row.nav));
    }
    const auto T = static_cast<std::int64_t>(equity.size());
    std::vector<double> r, x;
    for (std::int64_t d = 1; d <= T; ++d) {
        const double rd = navs[d] / navs[d - 1] - 1;
        r.push_back(rd);
        x.push_back(rd - rate_on(risk_free, iso_date(equity[d - 1].trade_date)) / 252);
    }

    std::optional<double> sharpe, sortino, vol;
    if (T >= 2) {
        double s = 0.0;
        for (double v : x) {
            s += v;
        }
        const double mean = s / T;
        double ss = 0.0;
        for (double v : x) {
            ss += std::pow(v - mean, 2);
        }
        double sd = std::sqrt(ss / (T - 1));
        if (*std::min_element(x.begin(), x.end()) == *std::max_element(x.begin(), x.end())) {
            sd = 0.0;  // all equal: the deviation is exactly 0, not rounding noise
        }
        if (sd != 0) {
            sharpe = mean / sd * std::sqrt(252.0);
        }
        double dn = 0.0;
        for (double v : x) {
            const double m = std::min(v, 0.0);
            dn += std::pow(m, 2);
        }
        const double den = std::sqrt(dn / T);
        if (den != 0) {
            sortino = mean / den * std::sqrt(252.0);
        }
        double sr = 0.0;
        for (double v : r) {
            sr += v;
        }
        const double mr = sr / T;
        double ssr = 0.0;
        for (double v : r) {
            ssr += std::pow(v - mr, 2);
        }
        vol = *std::min_element(r.begin(), r.end()) == *std::max_element(r.begin(), r.end())
                  ? 0.0
                  : std::sqrt(ssr / (T - 1)) * std::sqrt(252.0);
    }
    std::optional<double> cagr;
    if (T >= 1 && navs[0] > 0 && navs[T] > 0) {
        cagr = std::pow(navs[T] / navs[0], 252.0 / static_cast<double>(T)) - 1;
    }

    double peak = navs[0];
    double max_dd = 0.0;
    std::int64_t dur = 0, longest = 0;
    for (std::int64_t d = 1; d <= T; ++d) {
        if (navs[d] >= peak) {
            peak = navs[d];
            dur = 0;
        } else {
            ++dur;
            longest = std::max(longest, dur);
            const double dd = 1 - navs[d] / peak;
            max_dd = std::max(max_dd, dd);
        }
    }
    const std::optional<double> calmar = cagr && max_dd != 0 ? std::optional<double>(*cagr / max_dd) : std::nullopt;

    // Trades: FIFO matching of closing fills (and expiry settlements, D-45) against opening fills per
    // (instrument, product).
    struct Flow {
        TimeNs t;
        int is_settle;
        std::size_t n;
        InstrumentId iid;
        Product product;
        Side side;
        Qty qty;
        Paise price;
    };
    std::vector<Flow> flow;
    const Book& book = e.book();
    for (std::size_t n = 0; n < book.fills.size(); ++n) {
        const Fill& f = book.fills[n];
        flow.push_back(Flow{f.t_ns, 0, n, f.instrument, book.orders.at(f.order_id).product, f.side, f.qty, f.price});
    }
    for (std::size_t n = 0; n < e.settle_closes().size(); ++n) {
        const SettleClose& c = e.settle_closes()[n];
        flow.push_back(Flow{c.t, 1, n, c.instrument, c.product, c.side, c.units, c.price});
    }
    std::stable_sort(flow.begin(), flow.end(), [](const Flow& a, const Flow& b) {
        return std::tie(a.t, a.is_settle, a.n) < std::tie(b.t, b.is_settle, b.n);
    });
    std::map<std::pair<InstrumentId, std::string>, std::deque<std::tuple<int, Qty, Paise>>> lots;
    std::vector<Int128> trades;
    Int128 traded_value = 0;
    for (const Flow& fl : flow) {
        if (fl.is_settle == 0) {
            traded_value += Int128::mul(fl.qty, fl.price);
        }
        auto& q = lots[{fl.iid, std::string(to_string(fl.product))}];
        const int sign = fl.side == Side::Buy ? 1 : -1;
        Qty left = fl.qty;
        while (left != 0 && !q.empty() && std::get<0>(q.front()) != sign) {
            const auto [open_sign, open_qty, open_price] = q.front();
            const Qty k = std::min(left, open_qty);
            trades.push_back(Int128::mul(open_sign * k, fl.price - open_price));
            left -= k;
            if (k == open_qty) {
                q.pop_front();
            } else {
                q.front() = {open_sign, open_qty - k, open_price};
            }
        }
        if (left != 0) {
            q.emplace_back(sign, left, fl.price);
        }
    }
    std::int64_t wins = 0, losses = 0;
    Int128 gross_win = 0, gross_loss = 0, total_pnl = 0;
    for (const Int128& t : trades) {
        total_pnl += t;
        if (t > Int128(0)) {
            ++wins;
            gross_win += t;
        } else if (t < Int128(0)) {
            ++losses;
            gross_loss += t;
        }
    }
    const auto n_trades = static_cast<std::int64_t>(trades.size());

    JValue by_comp = JValue::object();
    std::int64_t brokerage = 0, stt = 0, exchange = 0, sebi = 0, stamp = 0, gst = 0, dp = 0, other = 0, total_costs = 0;
    for (const ChargeRow& c : book.charges) {
        brokerage += c.parts.brokerage;
        stt += c.parts.stt;
        exchange += c.parts.exchange;
        sebi += c.parts.sebi;
        stamp += c.parts.stamp;
        gst += c.parts.gst;
        dp += c.parts.dp;
        other += c.parts.other;
        total_costs += c.total;
    }
    by_comp["brokerage"] = brokerage;
    by_comp["stt"] = stt;
    by_comp["exchange"] = exchange;
    by_comp["sebi"] = sebi;
    by_comp["stamp"] = stamp;
    by_comp["gst"] = gst;
    by_comp["dp"] = dp;
    by_comp["other"] = other;
    const std::int64_t gross = !equity.empty() ? equity.back().nav - capital + total_costs : 0;
    std::optional<double> mean_nav;
    if (T != 0) {
        double sn = 0.0;
        for (std::size_t i = 1; i < navs.size(); ++i) {
            sn += navs[i];
        }
        mean_nav = sn / T;
    }
    std::optional<double> turnover;
    if (T != 0 && mean_nav && *mean_nav != 0) {
        turnover = (traded_value.to_double() / *mean_nav) * 252 / T;
    }

    JValue rejects = JValue::object();
    for (const auto& [code, n] : e.rejects()) {
        rejects[code] = n;
    }
    JValue m = JValue::object();
    m["days"] = T;
    m["nav_start_paise"] = capital;
    m["nav_end_paise"] = !equity.empty() ? equity.back().nav : capital;
    m["cagr"] = opt(cagr);
    m["volatility"] = opt(vol);
    m["sharpe"] = opt(sharpe);
    m["sortino"] = opt(sortino);
    m["max_drawdown"] = T != 0 ? JValue(max_dd) : JValue();
    m["max_drawdown_days"] = T != 0 ? JValue(longest) : JValue();
    m["calmar"] = opt(calmar);
    m["trades"] = n_trades;
    m["win_rate"] = n_trades != 0 ? JValue(div(wins, n_trades)) : JValue();
    m["profit_factor"] = !gross_loss.is_zero() ? JValue(gross_win.to_double() / (-gross_loss).to_double()) : JValue();
    m["avg_trade_paise"] = n_trades != 0 ? JValue(total_pnl.to_double() / static_cast<double>(n_trades)) : JValue();
    m["exposure"] = opt(e.exposure());
    m["turnover"] = opt(turnover);
    m["costs_paise"] = by_comp;
    m["costs_total_paise"] = total_costs;
    m["gross_pnl_paise"] = gross;
    m["cost_share_of_gross"] = gross != 0 ? JValue(div(total_costs, gross)) : JValue();
    m["rejects"] = rejects;
    m["max_participation"] = div(e.participation_used(), 1'000'000);
    return m;
}

}  // namespace stocksim

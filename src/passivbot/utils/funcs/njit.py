from __future__ import annotations

import logging
import os

import numba
import numpy as np

from passivbot import numba_njit

log = logging.getLogger(__name__)

JIT_DISABLED = os.environ.get("NOJIT", "false") in ("true", "1")


@numba_njit
def round_dynamic(n: float, d: int):
    if n == 0.0:
        return n
    return round(n, d - int(np.floor(np.log10(abs(n)))) - 1)


@numba_njit
def round_up(n: float, step, safety_rounding=10) -> float:
    return np.round(  # type: ignore[no-any-return]
        np.ceil(np.round(n / step, safety_rounding)) * step, safety_rounding
    )


@numba_njit
def round_dn(n, step, safety_rounding=10) -> float:
    return np.round(  # type: ignore[no-any-return]
        np.floor(np.round(n / step, safety_rounding)) * step, safety_rounding
    )


@numba_njit
def round_(n, step, safety_rounding=10) -> float:
    return np.round(np.round(n / step) * step, safety_rounding)  # type: ignore[no-any-return]


@numba_njit
def calc_diff(x, y):
    return abs(x - y) / abs(y)


@numba_njit
def nan_to_0(x) -> float:
    return x if x == x else 0.0  # type: ignore[no-any-return]


@numba_njit
def calc_min_entry_qty(price, inverse, qty_step, min_qty, min_cost) -> float:
    return (  # type: ignore[no-any-return]
        min_qty
        if inverse
        else max(min_qty, round_up(min_cost / price if price > 0.0 else 0.0, qty_step))
    )


@numba_njit
def cost_to_qty(cost, price, inverse, c_mult):
    return cost * price / c_mult if inverse else (cost / price if price > 0.0 else 0.0)


@numba_njit
def qty_to_cost(qty, price, inverse, c_mult) -> float:
    return (  # type: ignore[no-any-return]
        (abs(qty / price) if price > 0.0 else 0.0) * c_mult if inverse else abs(qty * price)
    )


@numba_njit
def calc_ema(alpha, alpha_, prev_ema, new_val) -> float:
    return prev_ema * alpha_ + new_val * alpha  # type: ignore[no-any-return]


@numba_njit
def calc_samples(ticks: np.ndarray, sample_size_ms: int = 1000) -> np.ndarray:
    # ticks [[timestamp, qty, price]]
    sampled_timestamps = np.arange(
        ticks[0][0] // sample_size_ms * sample_size_ms,
        ticks[-1][0] // sample_size_ms * sample_size_ms + sample_size_ms,
        sample_size_ms,
    )
    samples = np.zeros((len(sampled_timestamps), 3))
    samples[:, 0] = sampled_timestamps
    ts = sampled_timestamps[0]
    i = 0
    k = 0
    while True:
        if ts == samples[k][0]:
            samples[k][1] += ticks[i][1]
            samples[k][2] = ticks[i][2]
            i += 1
            if i >= len(ticks):
                break
            ts = ticks[i][0] // sample_size_ms * sample_size_ms
        else:
            k += 1
            if k >= len(samples):
                break
            samples[k][2] = samples[k - 1][2]
    return samples


@numba_njit
def calc_emas(xs, spans):
    emas = np.zeros((len(xs), len(spans)))
    alphas = 2 / (spans + 1)
    alphas_ = 1 - alphas
    emas[0] = xs[0]
    for i in range(1, len(xs)):
        emas[i] = emas[i - 1] * alphas_ + xs[i] * alphas
    return emas


@numba_njit
def calc_long_pnl(
    entry_price: float, close_price: float, qty: float, inverse: bool, c_mult: float
) -> float:
    if inverse:
        if entry_price == 0.0 or close_price == 0.0:
            return 0.0
        return abs(qty) * c_mult * (1.0 / entry_price - 1.0 / close_price)
    else:
        return abs(qty) * (close_price - entry_price)


@numba_njit
def calc_short_pnl(
    entry_price: float, close_price: float, qty: float, inverse: bool, c_mult: float
) -> float:
    if inverse:
        if entry_price == 0.0 or close_price == 0.0:
            return 0.0
        return abs(qty) * c_mult * (1.0 / close_price - 1.0 / entry_price)
    else:
        return abs(qty) * (entry_price - close_price)


@numba_njit
def calc_equity(
    balance, long_psize, long_pprice, short_psize, short_pprice, last_price, inverse, c_mult
):
    equity = balance
    if long_pprice and long_psize:
        equity += calc_long_pnl(long_pprice, last_price, long_psize, inverse, c_mult)
    if short_pprice and short_psize:
        equity += calc_short_pnl(short_pprice, last_price, short_psize, inverse, c_mult)
    return equity


@numba_njit
def calc_new_psize_pprice(psize, pprice, qty, price, qty_step) -> tuple[float, float]:
    if qty == 0.0:
        return psize, pprice
    new_psize = round_(psize + qty, qty_step)
    if new_psize == 0.0:
        return 0.0, 0.0
    return new_psize, nan_to_0(pprice) * (psize / new_psize) + price * (qty / new_psize)


@numba_njit
def calc_wallet_exposure_if_filled(balance, psize, pprice, qty, price, inverse, c_mult, qty_step):
    psize, qty = round_(abs(psize), qty_step), round_(abs(qty), qty_step)
    new_psize, new_pprice = calc_new_psize_pprice(psize, pprice, qty, price, qty_step)
    return qty_to_cost(new_psize, new_pprice, inverse, c_mult) / balance


@numba_njit
def calc_long_close_grid(
    balance,
    psize,
    pprice,
    lowest_ask,
    ema_band_upper,
    inverse,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    exposure_limit,
    min_markup,
    markup_range,
    n_close_orders,
    auto_unstuck_exposure_threshold,
    auto_unstuck_ema_dist,
) -> list[tuple[float, float, str]]:
    if psize == 0.0:
        return [(0.0, 0.0, "")]
    minm = pprice * (1 + min_markup)
    raw_close_prices = np.linspace(
        minm, pprice * (1 + min_markup + markup_range), int(round(n_close_orders))
    )
    close_prices = []
    for p_ in raw_close_prices:
        price = round_up(p_, price_step)
        if price >= lowest_ask:
            close_prices.append(price)
    psize_ = round_dn(psize, qty_step)  # round up for spot
    closes = []
    if len(close_prices) == 0:
        return [(-psize, lowest_ask, "long_nclose")]
    exposure = qty_to_cost(psize, pprice, inverse, c_mult) / balance
    threshold = exposure_limit * (1 - auto_unstuck_exposure_threshold)
    if auto_unstuck_exposure_threshold != 0.0 and exposure > threshold:
        unstuck_close_price = max(
            lowest_ask, round_up(ema_band_upper * (1 + auto_unstuck_ema_dist), price_step)
        )
        if unstuck_close_price < close_prices[0]:
            unstuck_close_qty = find_long_close_qty_bringing_wallet_exposure_to_target(
                balance,
                psize_,
                pprice,
                threshold * 1.01,
                unstuck_close_price,
                inverse,
                qty_step,
                c_mult,
            )
            min_entry_qty = calc_min_entry_qty(
                unstuck_close_price, inverse, qty_step, min_qty, min_cost
            )
            if unstuck_close_qty >= min_entry_qty:
                psize_ = round_(psize_ - unstuck_close_qty, qty_step)
                if psize_ < min_entry_qty:
                    # close whole pos; include leftovers
                    return [(-round_dn(psize, qty_step), unstuck_close_price, "long_unstuck_close")]
                closes.append((-unstuck_close_qty, unstuck_close_price, "long_unstuck_close"))
    if len(close_prices) == 1:
        if psize_ >= calc_min_entry_qty(close_prices[0], inverse, qty_step, min_qty, min_cost):
            closes.append((-psize_, close_prices[0], "long_nclose"))
        return closes
    default_close_qty = round_dn(psize_ / len(close_prices), qty_step)
    for price in close_prices[:-1]:
        min_close_qty = calc_min_entry_qty(price, inverse, qty_step, min_qty, min_cost)
        if psize_ < min_close_qty:
            break
        close_qty = min(psize_, max(min_close_qty, default_close_qty))
        closes.append((-close_qty, price, "long_nclose"))
        psize_ = round_(psize_ - close_qty, qty_step)
    min_close_qty = calc_min_entry_qty(close_prices[-1], inverse, qty_step, min_qty, min_cost)
    if psize_ >= min_close_qty:
        closes.append((-psize_, close_prices[-1], "long_nclose"))
    elif len(closes) > 0:
        closes[-1] = (-round_(abs(closes[-1][0]) + psize_, qty_step), closes[-1][1], closes[-1][2])
    return closes



@numba_njit
def calc_short_close_grid(
    balance,
    short_psize,
    short_pprice,
    highest_bid,
    ema_band_lower,
    spot,
    inverse,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    wallet_exposure_limit,
    initial_qty_pct,
    min_markup,
    markup_range,
    n_close_orders,
    auto_unstuck_wallet_exposure_threshold,
    auto_unstuck_ema_dist,
) -> list[tuple[float, float, str]]:
    if short_psize == 0.0:
        return [(0.0, 0.0, "")]
    minm = short_pprice * (1 - min_markup)
    abs_short_psize = abs(short_psize)
    if spot and round_dn(abs_short_psize, qty_step) < calc_min_entry_qty(
        minm, inverse, qty_step, min_qty, min_cost
    ):
        return [(0.0, 0.0, "")]
    if (
        abs_short_psize
        < cost_to_qty(balance, short_pprice, inverse, c_mult)
        * wallet_exposure_limit
        * initial_qty_pct
        * 0.5
    ):
        # close entire pos at breakeven or better if psize < initial_qty * 0.5
        # assumes maker fee rate 0.001 for spot, 0.0002 for futures
        breakeven_markup = 0.0021 if spot else 0.00041
        close_price = min(highest_bid, round_dn(short_pprice * (1 - breakeven_markup), price_step))
        return [(round_(abs_short_psize, qty_step), close_price, "short_nclose")]
    close_prices = []
    for p in np.linspace(minm, short_pprice * (1 - min_markup - markup_range), n_close_orders):
        price_ = round_dn(p, price_step)
        if price_ <= highest_bid:
            close_prices.append(price_)
    if len(close_prices) == 0:
        return [(round_(abs_short_psize, qty_step), highest_bid, "short_nclose")]
    elif len(close_prices) == 1:
        return [(round_(abs_short_psize, qty_step), close_prices[0], "short_nclose")]
    else:
        short_closes = []
        wallet_exposure = qty_to_cost(short_psize, short_pprice, inverse, c_mult) / balance
        threshold = wallet_exposure_limit * (1 - auto_unstuck_wallet_exposure_threshold)
        if auto_unstuck_wallet_exposure_threshold != 0.0 and wallet_exposure > threshold:
            auto_unstuck_price = min(
                highest_bid,
                round_dn(ema_band_lower * (1 - auto_unstuck_ema_dist), price_step),
            )
            if auto_unstuck_price > close_prices[0]:
                auto_unstuck_qty = find_short_close_qty_bringing_wallet_exposure_to_target(
                    balance,
                    short_psize,
                    short_pprice,
                    threshold * 1.01,
                    auto_unstuck_price,
                    inverse,
                    qty_step,
                    c_mult,
                )
                if auto_unstuck_qty >= calc_min_entry_qty(
                    auto_unstuck_price, inverse, qty_step, min_qty, min_cost
                ):
                    short_closes.append(
                        (
                            auto_unstuck_qty,
                            auto_unstuck_price,
                            "short_unstuck_close",
                        )
                    )
                    abs_short_psize = max(0.0, round_(abs_short_psize - auto_unstuck_qty, qty_step))
        min_close_qty = calc_min_entry_qty(close_prices[0], inverse, qty_step, min_qty, min_cost)
        default_qty = round_dn(abs_short_psize / len(close_prices), qty_step)
        if default_qty == 0.0:
            return [(round_(abs_short_psize, qty_step), close_prices[0], "short_nclose")]
        default_qty = max(min_close_qty, default_qty)
        remaining = round_(abs_short_psize, qty_step)
        for close_price in close_prices:
            if remaining < max(
                [
                    min_close_qty,
                    cost_to_qty(balance, close_price, inverse, c_mult)
                    * wallet_exposure_limit
                    * initial_qty_pct
                    * 0.5,
                    default_qty * 0.5,
                ]
            ):
                break
            close_qty = min(remaining, max(default_qty, min_close_qty))
            short_closes.append((close_qty, close_price, "short_nclose"))
            remaining = round_(remaining - close_qty, qty_step)
        if remaining:
            if short_closes:
                short_closes[-1] = (
                    round_(short_closes[-1][0] + remaining, qty_step),
                    short_closes[-1][1],
                    short_closes[-1][2],
                )
            else:
                short_closes = [(abs_short_psize, close_prices[0], "short_nclose")]
        return short_closes


@numba_njit
def calc_upnl(
    long_psize: float,
    long_pprice: float,
    short_psize: float,
    short_pprice: float,
    last_price: float,
    inverse: bool,
    c_mult: float,
) -> float:
    long_pnl: float = calc_long_pnl(long_pprice, last_price, long_psize, inverse, c_mult)
    short_pnl: float = calc_short_pnl(short_pprice, last_price, short_psize, inverse, c_mult)
    return long_pnl + short_pnl


@numba_njit
def calc_emas_last(xs, spans):
    alphas = 2.0 / (spans + 1.0)
    alphas_ = 1.0 - alphas
    emas = np.repeat(xs[0], len(spans))
    for i in range(1, len(xs)):
        emas = emas * alphas_ + xs[i] * alphas
    return emas


@numba_njit
def calc_bankruptcy_price(
    balance, long_psize, long_pprice, short_psize, short_pprice, inverse, c_mult
):
    long_pprice = nan_to_0(long_pprice)
    short_pprice = nan_to_0(short_pprice)
    long_psize *= c_mult
    abs_short_psize = abs(short_psize) * c_mult
    if inverse:
        short_cost = abs_short_psize / short_pprice if short_pprice > 0.0 else 0.0
        long_cost = long_psize / long_pprice if long_pprice > 0.0 else 0.0
        denominator = short_cost - long_cost - balance
        if denominator == 0.0:
            return 0.0
        bankruptcy_price = (abs_short_psize - long_psize) / denominator
    else:
        denominator = long_psize - abs_short_psize
        if denominator == 0.0:
            return 0.0
        bankruptcy_price = (
            -balance + long_psize * long_pprice - abs_short_psize * short_pprice
        ) / denominator
    return max(0.0, bankruptcy_price)


@numba_njit
def basespace(start, end, base, n):
    if base == 1.0:
        return np.linspace(start, end, n)
    a = np.array([base ** i for i in range(n)])
    a = (a - a.min()) / (a.max() - a.min())
    return a * (end - start) + start


@numba_njit
def powspace(start, stop, power, num):
    start = np.power(start, 1 / float(power))
    stop = np.power(stop, 1 / float(power))
    return np.power(np.linspace(start, stop, num=num), power)


@numba_njit
def calc_m_b(x0, x1, y0, y1):
    denom = x1 - x0
    if denom == 0.0:
        # zero div, return high number
        m = 9.0e32
    else:
        m = (y1 - y0) / (x1 - x0)
    return m, y0 - m * x0


@numba_njit
def calc_initial_entry_qty(
    balance,
    initial_entry_price,
    inverse,
    qty_step,
    min_qty,
    min_cost,
    c_mult,
    wallet_exposure_limit,
    initial_qty_pct,
):
    return max(
        calc_min_entry_qty(initial_entry_price, inverse, qty_step, min_qty, min_cost),
        round_(
            cost_to_qty(
                balance * wallet_exposure_limit * initial_qty_pct,
                initial_entry_price,
                inverse,
                c_mult,
            ),
            qty_step,
        ),
    )


@numba_njit
def calc_long_entry_qty(psize, pprice, entry_price, eprice_pprice_diff):
    return -(
        psize
        * (entry_price * eprice_pprice_diff + entry_price - pprice)
        / (entry_price * eprice_pprice_diff)
    )


@numba_njit
def calc_short_entry_qty(psize, pprice, entry_price, eprice_pprice_diff):
    return -(
        (psize * (entry_price * (eprice_pprice_diff - 1) + pprice))
        / (entry_price * eprice_pprice_diff)
    )


@numba_njit
def calc_long_entry_price(psize, pprice, entry_qty, eprice_pprice_diff):
    return (psize * pprice) / (psize * eprice_pprice_diff + psize + entry_qty * eprice_pprice_diff)


@numba_njit
def interpolate(x, xs, ys):
    return np.sum(
        np.array(
            [
                np.prod(np.array([(x - xs[m]) / (xs[j] - xs[m]) for m in range(len(xs)) if m != j]))
                * ys[j]
                for j in range(len(xs))
            ]
        )
    )


@numba_njit
def find_long_close_qty_bringing_wallet_exposure_to_target(
    balance,
    psize,
    pprice,
    wallet_exposure_target,
    close_price,
    inverse,
    qty_step,
    c_mult,
) -> float:
    wallet_exposure = qty_to_cost(psize, pprice, inverse, c_mult) / balance
    if wallet_exposure <= wallet_exposure_target * 1.001:
        # exposure within 0.1% of target: return zero
        return 0.0
    guesses = []
    vals = []
    evals = []
    guesses.append(
        min(psize, max(0.0, round_(psize * (wallet_exposure_target / wallet_exposure), qty_step)))
    )
    vals.append(
        qty_to_cost(abs(psize) - guesses[-1], pprice, inverse, c_mult)
        / (balance + calc_long_pnl(pprice, close_price, guesses[-1], inverse, c_mult))
    )
    evals.append(abs(vals[-1] - wallet_exposure_target) / wallet_exposure_target)
    guesses.append(
        min(psize, max(0.0, round_(max(guesses[-1] * 1.2, guesses[-1] + qty_step), qty_step)))
    )
    if guesses[-1] == guesses[-2]:
        guesses[-1] = min(
            psize, max(0.0, round_(min(guesses[-1] * 0.8, guesses[-1] - qty_step), qty_step))
        )
    vals.append(
        qty_to_cost(abs(psize) - guesses[-1], pprice, inverse, c_mult)
        / (balance + calc_long_pnl(pprice, close_price, guesses[-1], inverse, c_mult))
    )
    evals.append(abs(vals[-1] - wallet_exposure_target) / wallet_exposure_target)
    for _ in range(15):
        if guesses[-1] == guesses[-2] or vals[-1] == vals[-2]:
            guesses[-1] = min(
                psize, abs(round_(max(guesses[-2] * 2, guesses[-2] + qty_step * 10), qty_step))
            )
            vals[-1] = qty_to_cost(abs(psize) - guesses[-1], pprice, inverse, c_mult) / (
                balance + calc_long_pnl(pprice, close_price, guesses[-1], inverse, c_mult)
            )
        try:
            new_guess = interpolate(
                wallet_exposure_target, np.array(vals[-2:]), np.array(guesses[-2:])
            )
        except:
            print("debug zero div error find_long_close_qty_bringing_wallet_exposure_to_target")
            print(
                "balance, psize, pprice, wallet_exposure_target, close_price, inverse, qty_step, c_mult,"
            )
            print(
                balance,
                psize,
                pprice,
                wallet_exposure_target,
                close_price,
                inverse,
                qty_step,
                c_mult,
            )
            print("guesses, vals", guesses, vals)
            new_guess = round_(psize / 2, qty_step)
        guesses.append(min(psize, max(0.0, round_(new_guess, qty_step))))
        vals.append(
            qty_to_cost(abs(psize) - guesses[-1], pprice, inverse, c_mult)
            / (balance + calc_long_pnl(pprice, close_price, guesses[-1], inverse, c_mult))
        )
        evals.append(abs(vals[-1] - wallet_exposure_target) / wallet_exposure_target)
        if evals[-1] < 0.04:
            # close enough
            break
    evals_guesses = sorted([(e, g) for e, g in zip(evals, guesses)])
    if evals_guesses[0][0] > 0.15:
        print("debug find_long_close_qty_bringing_wallet_exposure_to_target")
        print(
            "balance, psize, pprice, wallet_exposure_target, close_price, inverse, qty_step, c_mult,"
        )
        print(
            balance,
            psize,
            pprice,
            wallet_exposure_target,
            close_price,
            inverse,
            qty_step,
            c_mult,
        )
        print("wallet_exposure", wallet_exposure)
        print("wallet_exposure_target", wallet_exposure_target)
        print(
            "guess, val, target diff",
            [(g, round_dynamic(v, 4), round_dynamic(e, 4)) for g, v, e in zip(guesses, vals, evals)],
        )
        print("n tries", len(guesses))
        print()
    return evals_guesses[0][1]


@numba_njit
def find_short_close_qty_bringing_wallet_exposure_to_target(
    balance,
    psize,
    pprice,
    wallet_exposure_target,
    close_price,
    inverse,
    qty_step,
    c_mult,
) -> float:
    wallet_exposure = qty_to_cost(psize, pprice, inverse, c_mult) / balance
    if wallet_exposure <= wallet_exposure_target * 1.001:
        # exposure within 0.1% of target: return zero
        return 0.0
    guesses = []
    vals = []
    evals = []
    abs_psize = abs(psize)
    guesses.append(
        min(
            abs_psize,
            max(0.0, round_(abs_psize * (wallet_exposure_target / wallet_exposure), qty_step)),
        )
    )
    vals.append(
        qty_to_cost(abs_psize - guesses[-1], pprice, inverse, c_mult)
        / (balance + calc_short_pnl(pprice, close_price, guesses[-1], inverse, c_mult))
    )
    evals.append(abs(vals[-1] - wallet_exposure_target) / wallet_exposure_target)
    guesses.append(
        min(abs_psize, max(0.0, round_(max(guesses[-1] * 1.2, guesses[-1] + qty_step), qty_step)))
    )
    if guesses[-1] == guesses[-2]:
        guesses[-1] = min(
            abs_psize, max(0.0, round_(min(guesses[-1] * 0.8, guesses[-1] - qty_step), qty_step))
        )
    vals.append(
        qty_to_cost(abs_psize - guesses[-1], pprice, inverse, c_mult)
        / (balance + calc_short_pnl(pprice, close_price, guesses[-1], inverse, c_mult))
    )
    evals.append(abs(vals[-1] - wallet_exposure_target) / wallet_exposure_target)
    for _ in range(15):
        if guesses[-1] == guesses[-2] or vals[-1] == vals[-2]:
            guesses[-1] = min(
                abs_psize, abs(round_(max(guesses[-2] * 1.1, guesses[-2] + qty_step), qty_step))
            )
            vals[-1] = qty_to_cost(abs_psize - guesses[-1], pprice, inverse, c_mult) / (
                balance + calc_short_pnl(pprice, close_price, guesses[-1], inverse, c_mult)
            )
        try:
            new_guess = interpolate(
                wallet_exposure_target, np.array(vals[-2:]), np.array(guesses[-2:])
            )
        except:
            print("debug zero div error find_short_close_qty_bringing_wallet_exposure_to_target")
            print(
                "balance, psize, pprice, wallet_exposure_target, close_price, inverse, qty_step, c_mult,"
            )
            print(
                balance,
                psize,
                pprice,
                wallet_exposure_target,
                close_price,
                inverse,
                qty_step,
                c_mult,
            )
            print("guesses, vals", guesses, vals)
            new_guess = round_(abs_psize / 2, qty_step)
        guesses.append(min(abs_psize, max(0.0, round_(new_guess, qty_step))))
        vals.append(
            qty_to_cost(abs_psize - guesses[-1], pprice, inverse, c_mult)
            / (balance + calc_short_pnl(pprice, close_price, guesses[-1], inverse, c_mult))
        )
        evals.append(abs(vals[-1] - wallet_exposure_target) / wallet_exposure_target)
        if evals[-1] < 0.04:
            # close enough
            break
    evals_guesses = sorted([(e, g) for e, g in zip(evals, guesses)])
    if evals_guesses[0][0] > 0.15:
        print("debug find_short_close_qty_bringing_wallet_exposure_to_target")
        print(
            "balance, psize, pprice, wallet_exposure_target, close_price, inverse, qty_step, c_mult,"
        )
        print(
            balance,
            psize,
            pprice,
            wallet_exposure_target,
            close_price,
            inverse,
            qty_step,
            c_mult,
        )
        print("wallet_exposure", wallet_exposure)
        print("wallet_exposure_target", wallet_exposure_target)
        print(
            "guess, val, target diff",
            [(g, round_dynamic(v, 4), round_dynamic(e, 4)) for g, v, e in zip(guesses, vals, evals)],
        )
        print("n tries", len(guesses))
        print()
    return evals_guesses[0][1]


@numba_njit
def find_entry_qty_bringing_wallet_exposure_to_target(
    balance,
    psize,
    pprice,
    wallet_exposure_target,
    entry_price,
    inverse,
    qty_step,
    c_mult,
) -> float:
    wallet_exposure = qty_to_cost(psize, pprice, inverse, c_mult) / balance
    if wallet_exposure >= wallet_exposure_target * 0.99:
        # return zero if exposure already is within 1% of target
        return 0.0
    guesses = []
    vals = []
    evals = []
    guesses.append(round_(abs(psize) * wallet_exposure_target / wallet_exposure, qty_step))
    vals.append(
        calc_wallet_exposure_if_filled(
            balance, psize, pprice, guesses[-1], entry_price, inverse, c_mult, qty_step
        )
    )
    evals.append(abs(vals[-1] - wallet_exposure_target) / wallet_exposure_target)
    guesses.append(max(0.0, round_(max(guesses[-1] * 1.2, guesses[-1] + qty_step), qty_step)))
    vals.append(
        calc_wallet_exposure_if_filled(
            balance, psize, pprice, guesses[-1], entry_price, inverse, c_mult, qty_step
        )
    )
    evals.append(abs(vals[-1] - wallet_exposure_target) / wallet_exposure_target)
    for _ in range(15):
        if guesses[-1] == guesses[-2]:
            guesses[-1] = abs(round_(max(guesses[-2] * 1.1, guesses[-2] + qty_step), qty_step))
            vals[-1] = calc_wallet_exposure_if_filled(
                balance, psize, pprice, guesses[-1], entry_price, inverse, c_mult, qty_step
            )
        guesses.append(
            max(
                0.0,
                round_(
                    interpolate(wallet_exposure_target, np.array(vals[-2:]), np.array(guesses[-2:])),
                    qty_step,
                ),
            )
        )
        vals.append(
            calc_wallet_exposure_if_filled(
                balance, psize, pprice, guesses[-1], entry_price, inverse, c_mult, qty_step
            )
        )
        evals.append(abs(vals[-1] - wallet_exposure_target) / wallet_exposure_target)
        if evals[-1] < 0.04:
            # close enough
            break
    evals_guesses = sorted([(e, g) for e, g in zip(evals, guesses)])
    if evals_guesses[0][0] > 0.15:
        print("debug find_entry_qty_bringing_wallet_exposure_to_target")
        print(
            "balance, psize, pprice, wallet_exposure_target, entry_price, inverse, qty_step, c_mult,"
        )
        print(
            balance,
            psize,
            pprice,
            wallet_exposure_target,
            entry_price,
            inverse,
            qty_step,
            c_mult,
        )
        print("wallet_exposure", wallet_exposure)
        print("wallet_exposure_target", wallet_exposure_target)
        print(
            "guess, val, target diff",
            [(g, round_dynamic(v, 4), round_dynamic(e, 4)) for g, v, e in zip(guesses, vals, evals)],
        )
        print()
    return evals_guesses[0][1]


@numba_njit
def find_eprice_pprice_diff_wallet_exposure_weighting(
    is_long: bool,
    balance,
    initial_entry_price,
    inverse,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    grid_span,
    wallet_exposure_limit,
    max_n_entry_orders,
    initial_qty_pct,
    eprice_pprice_diff,
    eprice_exp_base=1.618034,
    max_n_iters=20,
    error_tolerance=0.01,
    eprices=None,
    prev_pprice=None,
):
    def eval_(guess_):
        if is_long:
            return eval_long_entry_grid(
                balance,
                initial_entry_price,
                inverse,
                qty_step,
                price_step,
                min_qty,
                min_cost,
                c_mult,
                grid_span,
                wallet_exposure_limit,
                max_n_entry_orders,
                initial_qty_pct,
                eprice_pprice_diff,
                guess_,
                eprice_exp_base=eprice_exp_base,
                eprices=eprices,
                prev_pprice=prev_pprice,
            )[-1][4]
        else:
            return eval_short_entry_grid(
                balance,
                initial_entry_price,
                inverse,
                qty_step,
                price_step,
                min_qty,
                min_cost,
                c_mult,
                grid_span,
                wallet_exposure_limit,
                max_n_entry_orders,
                initial_qty_pct,
                eprice_pprice_diff,
                guess_,
                eprice_exp_base=eprice_exp_base,
                eprices=eprices,
                prev_pprice=prev_pprice,
            )[-1][4]

    guess = 0.0
    val = eval_(guess)
    if val < wallet_exposure_limit:
        return guess
    too_low = (guess, val)
    guess = 1000.0
    val = eval_(guess)
    if val > wallet_exposure_limit:
        guess = 10000.0
        val = eval_(guess)
        if val > wallet_exposure_limit:
            guess = 100000.0
            val = eval_(guess)
            if val > wallet_exposure_limit:
                return guess
    too_high = (guess, val)
    guesses = [too_low[1], too_high[1]]
    vals = [too_low[0], too_high[0]]
    guess = interpolate(wallet_exposure_limit, np.array(vals), np.array(guesses))
    val = eval_(guess)
    if val < wallet_exposure_limit:
        too_high = (guess, val)
    else:
        too_low = (guess, val)
    i = 0
    old_guess = 0.0
    best_guess = (abs(val - wallet_exposure_limit) / wallet_exposure_limit, guess, val)
    while True:
        i += 1
        diff = abs(val - wallet_exposure_limit) / wallet_exposure_limit
        if diff < best_guess[0]:
            best_guess = (diff, guess, val)
        if diff < error_tolerance:
            return best_guess[1]
        if i >= max_n_iters or abs(old_guess - guess) / guess < error_tolerance * 0.1:
            """
            if best_guess[0] > 0.15:
                log.info('debug find_eprice_pprice_diff_wallet_exposure_weighting')
                log.info('is_long, balance, initial_entry_price, inverse, qty_step, price_step, min_qty, min_cost, c_mult, grid_span, wallet_exposure_limit, max_n_entry_orders, initial_qty_pct, eprice_pprice_diff, eprice_exp_base, max_n_iters, error_tolerance, eprices, prev_pprice')
                log.info(is_long, ',', balance, ',', initial_entry_price, ',', inverse, ',', qty_step, ',', price_step, ',', min_qty, ',', min_cost, ',', c_mult, ',', grid_span, ',', wallet_exposure_limit, ',', max_n_entry_orders, ',', initial_qty_pct, ',', eprice_pprice_diff, ',', eprice_exp_base, ',', max_n_iters, ',', error_tolerance, ',', eprices, ',', prev_pprice)
            """
            return best_guess[1]
        old_guess = guess
        guess = (too_high[0] + too_low[0]) / 2
        val = eval_(guess)
        if val < wallet_exposure_limit:
            too_high = (guess, val)
        else:
            too_low = (guess, val)


@numba_njit
def eval_long_entry_grid(
    balance,
    initial_entry_price,
    inverse,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    grid_span,
    wallet_exposure_limit,
    max_n_entry_orders,
    initial_qty_pct,
    eprice_pprice_diff,
    eprice_pprice_diff_wallet_exposure_weighting,
    eprice_exp_base=1.618034,
    eprices=None,
    prev_pprice=None,
):

    # returns [qty, price, psize, pprice, wallet_exposure]
    if eprices is None:
        grid = np.zeros((max_n_entry_orders, 5))
        grid[:, 1] = [
            round_dn(p, price_step)
            for p in basespace(
                initial_entry_price,
                initial_entry_price * (1 - grid_span),
                eprice_exp_base,
                max_n_entry_orders,
            )
        ]
    else:
        max_n_entry_orders = len(eprices)
        grid = np.zeros((max_n_entry_orders, 5))
        grid[:, 1] = eprices

    grid[0][0] = max(
        calc_min_entry_qty(grid[0][1], inverse, qty_step, min_qty, min_cost),
        round_(
            cost_to_qty(
                balance * wallet_exposure_limit * initial_qty_pct,
                initial_entry_price,
                inverse,
                c_mult,
            ),
            qty_step,
        ),
    )
    grid[0][2] = psize = grid[0][0]
    grid[0][3] = pprice = grid[0][1] if prev_pprice is None else prev_pprice
    grid[0][4] = qty_to_cost(psize, pprice, inverse, c_mult) / balance
    for i in range(1, max_n_entry_orders):
        adjusted_eprice_pprice_diff = eprice_pprice_diff * (
            1 + grid[i - 1][4] * eprice_pprice_diff_wallet_exposure_weighting
        )
        qty = round_(
            calc_long_entry_qty(psize, pprice, grid[i][1], adjusted_eprice_pprice_diff),
            qty_step,
        )
        if qty < calc_min_entry_qty(grid[i][1], inverse, qty_step, min_qty, min_cost):
            qty = 0.0
        psize, pprice = calc_new_psize_pprice(psize, pprice, qty, grid[i][1], qty_step)
        grid[i][0] = qty
        grid[i][2:] = [
            psize,
            pprice,
            qty_to_cost(psize, pprice, inverse, c_mult) / balance,
        ]
    return grid


@numba_njit
def eval_short_entry_grid(
    balance,
    initial_entry_price,
    inverse,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    grid_span,
    wallet_exposure_limit,
    max_n_entry_orders,
    initial_qty_pct,
    eprice_pprice_diff,
    eprice_pprice_diff_wallet_exposure_weighting,
    eprice_exp_base=1.618034,
    eprices=None,
    prev_pprice=None,
):

    # returns [qty, price, psize, pprice, wallet_exposure]
    if eprices is None:
        grid = np.zeros((max_n_entry_orders, 5))
        grid[:, 1] = [
            round_up(p, price_step)
            for p in basespace(
                initial_entry_price,
                initial_entry_price * (1 + grid_span),
                eprice_exp_base,
                max_n_entry_orders,
            )
        ]
    else:
        max_n_entry_orders = len(eprices)
        grid = np.zeros((max_n_entry_orders, 5))
        grid[:, 1] = eprices

    grid[0][0] = -max(
        calc_min_entry_qty(grid[0][1], inverse, qty_step, min_qty, min_cost),
        round_(
            cost_to_qty(
                balance * wallet_exposure_limit * initial_qty_pct,
                initial_entry_price,
                inverse,
                c_mult,
            ),
            qty_step,
        ),
    )
    grid[0][2] = psize = grid[0][0]
    grid[0][3] = pprice = grid[0][1] if prev_pprice is None else prev_pprice
    grid[0][4] = qty_to_cost(psize, pprice, inverse, c_mult) / balance
    for i in range(1, max_n_entry_orders):
        adjusted_eprice_pprice_diff = eprice_pprice_diff * (
            1 + grid[i - 1][4] * eprice_pprice_diff_wallet_exposure_weighting
        )
        qty = round_(
            calc_short_entry_qty(psize, pprice, grid[i][1], adjusted_eprice_pprice_diff),
            qty_step,
        )
        if -qty < calc_min_entry_qty(grid[i][1], inverse, qty_step, min_qty, min_cost):
            qty = 0.0
        psize, pprice = calc_new_psize_pprice(psize, pprice, qty, grid[i][1], qty_step)
        grid[i][0] = qty
        grid[i][2:] = [
            psize,
            pprice,
            qty_to_cost(psize, pprice, inverse, c_mult) / balance,
        ]
    return grid


@numba_njit
def calc_whole_long_entry_grid(
    balance,
    initial_entry_price,
    inverse,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    grid_span,
    wallet_exposure_limit,
    max_n_entry_orders,
    initial_qty_pct,
    eprice_pprice_diff,
    secondary_allocation,
    secondary_pprice_diff,
    eprice_exp_base=1.618034,
    eprices=None,
    prev_pprice=None,
):

    # [qty, price, psize, pprice, wallet_exposure]
    if secondary_allocation <= 0.05:
        # set to zero if secondary allocation less than 5%
        secondary_allocation = 0.0
    elif secondary_allocation >= 1.0:
        raise Exception("secondary_allocation cannot be >= 1.0")
    primary_wallet_exposure_allocation = 1.0 - secondary_allocation
    primary_wallet_exposure_limit = wallet_exposure_limit * primary_wallet_exposure_allocation
    eprice_pprice_diff_wallet_exposure_weighting = find_eprice_pprice_diff_wallet_exposure_weighting(
        True,
        balance,
        initial_entry_price,
        inverse,
        qty_step,
        price_step,
        min_qty,
        min_cost,
        c_mult,
        grid_span,
        primary_wallet_exposure_limit,
        max_n_entry_orders,
        initial_qty_pct / primary_wallet_exposure_allocation,
        eprice_pprice_diff,
        eprice_exp_base,
        eprices=eprices,
        prev_pprice=prev_pprice,
    )
    grid = eval_long_entry_grid(
        balance,
        initial_entry_price,
        inverse,
        qty_step,
        price_step,
        min_qty,
        min_cost,
        c_mult,
        grid_span,
        primary_wallet_exposure_limit,
        max_n_entry_orders,
        initial_qty_pct / primary_wallet_exposure_allocation,
        eprice_pprice_diff,
        eprice_pprice_diff_wallet_exposure_weighting,
        eprice_exp_base,
        eprices=eprices,
        prev_pprice=prev_pprice,
    )
    if secondary_allocation > 0.0:
        entry_price = min(
            round_dn(grid[-1][3] * (1 - secondary_pprice_diff), price_step), grid[-1][1]
        )
        qty = find_entry_qty_bringing_wallet_exposure_to_target(
            balance,
            grid[-1][2],
            grid[-1][3],
            wallet_exposure_limit,
            entry_price,
            inverse,
            qty_step,
            c_mult,
        )
        new_psize, new_pprice = calc_new_psize_pprice(
            grid[-1][2], grid[-1][3], qty, entry_price, qty_step
        )
        new_wallet_exposure = qty_to_cost(new_psize, new_pprice, inverse, c_mult) / balance
        grid = np.append(
            grid,
            np.array([[qty, entry_price, new_psize, new_pprice, new_wallet_exposure]]),
            axis=0,
        )
    return grid[grid[:, 0] > 0.0]


@numba_njit
def calc_whole_short_entry_grid(
    balance,
    initial_entry_price,
    inverse,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    grid_span,
    wallet_exposure_limit,
    max_n_entry_orders,
    initial_qty_pct,
    eprice_pprice_diff,
    secondary_allocation,
    secondary_pprice_diff,
    eprice_exp_base=1.618034,
    eprices=None,
    prev_pprice=None,
):

    # [qty, price, psize, pprice, wallet_exposure]
    if secondary_allocation <= 0.05:
        # set to zero if secondary allocation less than 5%
        secondary_allocation = 0.0
    elif secondary_allocation >= 1.0:
        raise Exception("secondary_allocation cannot be >= 1.0")
    primary_wallet_exposure_allocation = 1.0 - secondary_allocation
    primary_wallet_exposure_limit = wallet_exposure_limit * primary_wallet_exposure_allocation
    eprice_pprice_diff_wallet_exposure_weighting = find_eprice_pprice_diff_wallet_exposure_weighting(
        False,
        balance,
        initial_entry_price,
        inverse,
        qty_step,
        price_step,
        min_qty,
        min_cost,
        c_mult,
        grid_span,
        primary_wallet_exposure_limit,
        max_n_entry_orders,
        initial_qty_pct / primary_wallet_exposure_allocation,
        eprice_pprice_diff,
        eprice_exp_base,
        eprices=eprices,
        prev_pprice=prev_pprice,
    )
    grid = eval_short_entry_grid(
        balance,
        initial_entry_price,
        inverse,
        qty_step,
        price_step,
        min_qty,
        min_cost,
        c_mult,
        grid_span,
        primary_wallet_exposure_limit,
        max_n_entry_orders,
        initial_qty_pct / primary_wallet_exposure_allocation,
        eprice_pprice_diff,
        eprice_pprice_diff_wallet_exposure_weighting,
        eprice_exp_base,
        eprices=eprices,
        prev_pprice=prev_pprice,
    )
    if secondary_allocation > 0.0:
        entry_price = max(
            round_up(grid[-1][3] * (1 + secondary_pprice_diff), price_step), grid[-1][1]
        )
        qty = -find_entry_qty_bringing_wallet_exposure_to_target(
            balance,
            grid[-1][2],
            grid[-1][3],
            wallet_exposure_limit,
            entry_price,
            inverse,
            qty_step,
            c_mult,
        )
        new_psize, new_pprice = calc_new_psize_pprice(
            grid[-1][2], grid[-1][3], qty, entry_price, qty_step
        )
        new_wallet_exposure = qty_to_cost(new_psize, new_pprice, inverse, c_mult) / balance
        grid = np.append(
            grid,
            np.array([[qty, entry_price, new_psize, new_pprice, new_wallet_exposure]]),
            axis=0,
        )
    return grid[grid[:, 0] < 0.0]


@numba_njit
def calc_long_entry_grid(
    balance,
    psize,
    pprice,
    highest_bid,
    ema_band_lower,
    inverse,
    do_long,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    grid_span,
    wallet_exposure_limit,
    max_n_entry_orders,
    initial_qty_pct,
    initial_eprice_ema_dist,
    eprice_pprice_diff,
    secondary_allocation,
    secondary_pprice_diff,
    eprice_exp_base,
    auto_unstuck_wallet_exposure_threshold,
    auto_unstuck_ema_dist,
) -> [(float, float, str)]:
    min_entry_qty = calc_min_entry_qty(highest_bid, inverse, qty_step, min_qty, min_cost)
    if do_long or psize > min_entry_qty:
        if psize == 0.0:
            entry_price = min(
                highest_bid,
                round_dn(ema_band_lower * (1 - initial_eprice_ema_dist), price_step),
            )
            entry_qty = calc_initial_entry_qty(
                balance,
                entry_price,
                inverse,
                qty_step,
                min_qty,
                min_cost,
                c_mult,
                wallet_exposure_limit,
                initial_qty_pct,
            )
            return [(entry_qty, entry_price, "long_ientry")]
        else:
            wallet_exposure = qty_to_cost(psize, pprice, inverse, c_mult) / balance
            if wallet_exposure >= wallet_exposure_limit:
                return [(0.0, 0.0, "")]
            if auto_unstuck_wallet_exposure_threshold != 0.0:
                threshold = (
                    wallet_exposure_limit * (1 - auto_unstuck_wallet_exposure_threshold) * 0.99
                )
                if wallet_exposure > threshold:
                    auto_unstuck_entry_price = min(
                        highest_bid,
                        round_dn(ema_band_lower * (1 - auto_unstuck_ema_dist), price_step),
                    )
                    auto_unstuck_qty = find_entry_qty_bringing_wallet_exposure_to_target(
                        balance,
                        psize,
                        pprice,
                        wallet_exposure_limit,
                        auto_unstuck_entry_price,
                        inverse,
                        qty_step,
                        c_mult,
                    )
                    return [
                        (
                            auto_unstuck_qty,
                            auto_unstuck_entry_price,
                            "long_unstuck_entry",
                        )
                    ]
            grid = approximate_long_grid(
                balance,
                psize,
                pprice,
                inverse,
                qty_step,
                price_step,
                min_qty,
                min_cost,
                c_mult,
                grid_span,
                wallet_exposure_limit,
                max_n_entry_orders,
                initial_qty_pct,
                eprice_pprice_diff,
                secondary_allocation,
                secondary_pprice_diff,
                eprice_exp_base=eprice_exp_base,
            )
            if len(grid) == 0:
                return [(0.0, 0.0, "")]
            if calc_diff(grid[0][3], grid[0][1]) < 0.00001:
                # means initial entry was partially filled
                entry_price = min(
                    highest_bid,
                    round_dn(ema_band_lower * (1 - initial_eprice_ema_dist), price_step),
                )
                min_entry_qty = calc_min_entry_qty(entry_price, inverse, qty_step, min_qty, min_cost)
                max_entry_qty = round_(
                    cost_to_qty(
                        balance * wallet_exposure_limit * initial_qty_pct,
                        entry_price,
                        inverse,
                        c_mult,
                    ),
                    qty_step,
                )
                entry_qty = max(min_entry_qty, min(max_entry_qty, grid[0][0]))
                                if (
                    qty_to_cost(entry_qty, entry_price, inverse, c_mult) / balance
                    > wallet_exposure_limit * 1.1
                ):
                    if JIT_DISABLED:
                        with numba.objmode():
                            log.warning(
                                "abnormally large partial ientry.\nGrid:\n%s",
                                "\n".join([str(e) for e in grid]),
                            )
                            log.info(
                                "Args:\n%s",
                                "\n".join(
                                    str(arg)
                                    for arg in (
                                        balance,
                                        psize,
                                        pprice,
                                        highest_bid,
                                        inverse,
                                        do_long,
                                        qty_step,
                                        price_step,
                                        min_qty,
                                        min_cost,
                                        c_mult,
                                        grid_span,
                                        wallet_exposure_limit,
                                        max_n_entry_orders,
                                        initial_qty_pct,
                                        eprice_pprice_diff,
                                        secondary_allocation,
                                        secondary_pprice_diff,
                                        eprice_exp_base,
                                    )
                                ),
                            )
                return [(entry_qty, entry_price, "long_ientry")]
        if len(grid) == 0:
            return [(0.0, 0.0, "")]
        entries = []
        for i in range(len(grid)):
            if grid[i][2] < psize * 1.05 or grid[i][1] > pprice * 0.9995:
                continue
            if grid[i][4] > wallet_exposure_limit * 1.01:
                break
            entry_price = min(highest_bid, grid[i][1])
            min_entry_qty = calc_min_entry_qty(entry_price, inverse, qty_step, min_qty, min_cost)
            grid[i][1] = entry_price
            grid[i][0] = max(min_entry_qty, grid[i][0])
            comment = (
                "long_secondary_rentry"
                if i == len(grid) - 1 and secondary_allocation > 0.05
                else "long_primary_rentry"
            )
            if not entries or (entries[-1][1] != entry_price):
                entries.append((grid[i][0], grid[i][1], comment))
        return entries if entries else [(0.0, 0.0, "")]
    return [(0.0, 0.0, "")]


@numba_njit
def calc_short_entry_grid(
    balance,
    psize,
    pprice,
    lowest_ask,
    ema_band_upper,
    inverse,
    do_short,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    grid_span,
    wallet_exposure_limit,
    max_n_entry_orders,
    initial_qty_pct,
    initial_eprice_ema_dist,
    eprice_pprice_diff,
    secondary_allocation,
    secondary_pprice_diff,
    eprice_exp_base,
    auto_unstuck_wallet_exposure_threshold,
    auto_unstuck_ema_dist,
) -> [(float, float, str)]:
    min_entry_qty = calc_min_entry_qty(lowest_ask, inverse, qty_step, min_qty, min_cost)
    abs_psize = abs(psize)
    if do_short or abs_psize > min_entry_qty:
        if psize == 0.0:

            entry_price = max(
                lowest_ask,
                round_up(ema_band_upper * (1 + initial_eprice_ema_dist), price_step),
            )
            entry_qty = calc_initial_entry_qty(
                balance,
                entry_price,
                inverse,
                qty_step,
                min_qty,
                min_cost,
                c_mult,
                wallet_exposure_limit,
                initial_qty_pct,
            )
            return [(-entry_qty, entry_price, "short_ientry")]
        else:
            wallet_exposure = qty_to_cost(psize, pprice, inverse, c_mult) / balance
            if wallet_exposure >= wallet_exposure_limit:
                return [(0.0, 0.0, "")]
            if auto_unstuck_wallet_exposure_threshold != 0.0:
                threshold = (
                    wallet_exposure_limit * (1 - auto_unstuck_wallet_exposure_threshold) * 0.99
                )
                if wallet_exposure > threshold:
                    auto_unstuck_entry_price = max(
                        lowest_ask,
                        round_up(ema_band_upper * (1 + auto_unstuck_ema_dist), price_step),
                    )
                    auto_unstuck_qty = find_entry_qty_bringing_wallet_exposure_to_target(
                        balance,
                        psize,
                        pprice,
                        wallet_exposure_limit,
                        auto_unstuck_entry_price,
                        inverse,
                        qty_step,
                        c_mult,
                    )
                    return [
                        (
                            -auto_unstuck_qty,
                            auto_unstuck_entry_price,
                            "short_unstuck_entry",
                        )
                    ]
            grid = approximate_short_grid(
                balance,
                psize,
                pprice,
                inverse,
                qty_step,
                price_step,
                min_qty,
                min_cost,
                c_mult,
                grid_span,
                wallet_exposure_limit,
                max_n_entry_orders,
                initial_qty_pct,
                eprice_pprice_diff,
                secondary_allocation,
                secondary_pprice_diff,
                eprice_exp_base=eprice_exp_base,
            )
            if len(grid) == 0:
                return [(0.0, 0.0, "")]
            if calc_diff(grid[0][3], grid[0][1]) < 0.00001:
                # means initial entry was partially filled
                entry_price = max(
                    lowest_ask,
                    round_up(ema_band_upper * (1 + initial_eprice_ema_dist), price_step),
                )
                min_entry_qty = calc_min_entry_qty(entry_price, inverse, qty_step, min_qty, min_cost)
                max_entry_qty = round_(
                    cost_to_qty(
                        balance * wallet_exposure_limit * initial_qty_pct,
                        entry_price,
                        inverse,
                        c_mult,
                    ),
                    qty_step,
                )
                entry_qty = -max(min_entry_qty, min(max_entry_qty, abs(grid[0][0])))
                if (
                    qty_to_cost(entry_qty, entry_price, inverse, c_mult) / balance
                    > wallet_exposure_limit * 1.1
                ):
                    print("\n\nwarning: abnormally large partial ientry")
                    print("grid:")
                    for e in grid:
                        print(list(e))
                    print("args:")
                    print(
                        balance,
                        psize,
                        pprice,
                        lowest_ask,
                        inverse,
                        do_short,
                        qty_step,
                        price_step,
                        min_qty,
                        min_cost,
                        c_mult,
                        grid_span,
                        wallet_exposure_limit,
                        max_n_entry_orders,
                        initial_qty_pct,
                        eprice_pprice_diff,
                        secondary_allocation,
                        secondary_pprice_diff,
                        eprice_exp_base,
                    )
                    print("\n\n")
                return [(entry_qty, entry_price, "short_ientry")]
        if len(grid) == 0:
            return [(0.0, 0.0, "")]
        entries = []
        for i in range(len(grid)):
            if grid[i][2] > psize * 1.05 or grid[i][1] < pprice * 0.9995:
                continue
            entry_price = max(lowest_ask, grid[i][1])
            min_entry_qty = calc_min_entry_qty(entry_price, inverse, qty_step, min_qty, min_cost)
            grid[i][1] = entry_price
            grid[i][0] = -max(min_entry_qty, abs(grid[i][0]))
            comment = (
                "short_secondary_rentry"
                if i == len(grid) - 1 and secondary_allocation > 0.05
                else "short_primary_rentry"
            )
            if not entries or (entries[-1][1] != entry_price):
                entries.append((grid[i][0], grid[i][1], comment))
        return entries if entries else [(0.0, 0.0, "")]
    return [(0.0, 0.0, "")]


@numba_njit
def approximate_long_grid(
    balance,
    psize,
    pprice,
    inverse,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    grid_span,
    wallet_exposure_limit,
    max_n_entry_orders,
    initial_qty_pct,
    eprice_pprice_diff,
    secondary_allocation,
    secondary_pprice_diff,
    eprice_exp_base=1.618034,
    crop: bool = True,
):
    def eval_(ientry_price_guess, psize_):
        ientry_price_guess = round_(ientry_price_guess, price_step)
        grid = calc_whole_long_entry_grid(
            balance,
            ientry_price_guess,
            inverse,
            qty_step,
            price_step,
            min_qty,
            min_cost,
            c_mult,
            grid_span,
            wallet_exposure_limit,
            max_n_entry_orders,
            initial_qty_pct,
            eprice_pprice_diff,
            secondary_allocation,
            secondary_pprice_diff,
            eprice_exp_base=eprice_exp_base,
        )
        # find node whose psize is closest to psize
        diff, i = sorted([(abs(grid[i][2] - psize_) / psize_, i) for i in range(len(grid))])[0]
        return grid, diff, i

    if pprice == 0.0:
        raise Exception("cannot make grid without pprice")
    if psize == 0.0:
        return calc_whole_long_entry_grid(
            balance,
            pprice,
            inverse,
            qty_step,
            price_step,
            min_qty,
            min_cost,
            c_mult,
            grid_span,
            wallet_exposure_limit,
            max_n_entry_orders,
            initial_qty_pct,
            eprice_pprice_diff,
            secondary_allocation,
            secondary_pprice_diff,
            eprice_exp_base=eprice_exp_base,
        )

    grid, diff, i = eval_(pprice, psize)
    grid, diff, i = eval_(pprice * (pprice / grid[i][3]), psize)
    if diff < 0.01:
        # good guess
        grid, diff, i = eval_(grid[0][1] * (pprice / grid[i][3]), psize)
        return grid[i + 1 :] if crop else grid
    # no close matches
    # assume partial fill
    k = 0
    while k < len(grid) - 1 and grid[k][2] <= psize * 0.99999:
        # find first node whose psize > psize
        k += 1
    if k == 0:
        # means psize is less than iqty
        # return grid with adjusted iqty
        min_ientry_qty = calc_min_entry_qty(grid[0][1], inverse, qty_step, min_qty, min_cost)
        grid[0][0] = max(min_ientry_qty, round_(grid[0][0] - psize, qty_step))
        grid[0][2] = round_(psize + grid[0][0], qty_step)
        grid[0][4] = qty_to_cost(grid[0][2], grid[0][3], inverse, c_mult) / balance
        return grid
    if k == len(grid):
        # means wallet_exposure limit is exceeded
        return np.empty((0, 5)) if crop else grid
    for _ in range(5):
        # find grid as if partial fill were full fill
        remaining_qty = round_(grid[k][2] - psize, qty_step)
        npsize, npprice = calc_new_psize_pprice(psize, pprice, remaining_qty, grid[k][1], qty_step)
        grid, diff, i = eval_(npprice, npsize)
        if k >= len(grid):
            k = len(grid) - 1
            continue
        grid, diff, i = eval_(npprice * (npprice / grid[k][3]), npsize)
        k = 0
        while k < len(grid) - 1 and grid[k][2] <= psize * 0.99999:
            # find first node whose psize > psize
            k += 1
    min_entry_qty = calc_min_entry_qty(grid[k][1], inverse, qty_step, min_qty, min_cost)
    grid[k][0] = max(min_entry_qty, round_(grid[k][2] - psize, qty_step))
    return grid[k:] if crop else grid


@numba_njit
def approximate_short_grid(
    balance,
    psize,
    pprice,
    inverse,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    grid_span,
    wallet_exposure_limit,
    max_n_entry_orders,
    initial_qty_pct,
    eprice_pprice_diff,
    secondary_allocation,
    secondary_pprice_diff,
    eprice_exp_base=1.618034,
    crop: bool = True,
):
    def eval_(ientry_price_guess, psize_):
        ientry_price_guess = round_(ientry_price_guess, price_step)
        grid = calc_whole_short_entry_grid(
            balance,
            ientry_price_guess,
            inverse,
            qty_step,
            price_step,
            min_qty,
            min_cost,
            c_mult,
            grid_span,
            wallet_exposure_limit,
            max_n_entry_orders,
            initial_qty_pct,
            eprice_pprice_diff,
            secondary_allocation,
            secondary_pprice_diff,
            eprice_exp_base=eprice_exp_base,
        )
        # find node whose psize is closest to psize
        abs_psize_ = abs(psize_)
        diff, i = sorted(
            [(abs(abs(grid[i][2]) - abs_psize_) / abs_psize_, i) for i in range(len(grid))]
        )[0]
        return grid, diff, i

    abs_psize = abs(psize)

    if pprice == 0.0:
        raise Exception("cannot make grid without pprice")
    if psize == 0.0:
        return calc_whole_short_entry_grid(
            balance,
            pprice,
            inverse,
            qty_step,
            price_step,
            min_qty,
            min_cost,
            c_mult,
            grid_span,
            wallet_exposure_limit,
            max_n_entry_orders,
            initial_qty_pct,
            eprice_pprice_diff,
            secondary_allocation,
            secondary_pprice_diff,
            eprice_exp_base=eprice_exp_base,
        )

    grid, diff, i = eval_(pprice, psize)
    grid, diff, i = eval_(pprice * (pprice / grid[i][3]), psize)
    if diff < 0.01:
        # good guess
        grid, diff, i = eval_(grid[0][1] * (pprice / grid[i][3]), psize)
        return grid[i + 1 :] if crop else grid
    # no close matches
    # assume partial fill
    k = 0
    while k < len(grid) - 1 and abs(grid[k][2]) <= abs_psize * 0.99999:
        # find first node whose psize > psize
        k += 1
    if k == 0:
        # means psize is less than iqty
        # return grid with adjusted iqty
        min_ientry_qty = calc_min_entry_qty(grid[0][1], inverse, qty_step, min_qty, min_cost)
        grid[0][0] = -max(min_ientry_qty, round_(abs(grid[0][0]) - abs_psize, qty_step))
        grid[0][2] = round_(psize + grid[0][0], qty_step)
        grid[0][4] = qty_to_cost(grid[0][2], grid[0][3], inverse, c_mult) / balance
        return grid
    if k == len(grid):
        # means wallet_exposure limit is exceeded
        return np.empty((0, 5)) if crop else grid
    for _ in range(5):
        # find grid as if partial fill were full fill
        remaining_qty = round_(grid[k][2] - psize, qty_step)
        npsize, npprice = calc_new_psize_pprice(psize, pprice, remaining_qty, grid[k][1], qty_step)
        grid, diff, i = eval_(npprice, npsize)
        if k >= len(grid):
            k = len(grid) - 1
            continue
        grid, diff, i = eval_(npprice * (npprice / grid[k][3]), npsize)
        k = 0
        while k < len(grid) - 1 and abs(grid[k][2]) <= abs_psize * 0.99999:
            # find first node whose psize > psize
            k += 1
    min_entry_qty = calc_min_entry_qty(grid[k][1], inverse, qty_step, min_qty, min_cost)
    grid[k][0] = -max(min_entry_qty, round_(abs(grid[k][2]) - abs_psize, qty_step))
    return grid[k:] if crop else grid


@numba_njit
def njit_backtest(
    ticks,
    starting_balance,
    latency_simulation_ms,
    maker_fee,
    spot,
    hedge_mode,
    inverse,
    do_long,
    do_short,
    qty_step,
    price_step,
    min_qty,
    min_cost,
    c_mult,
    ema_span_max,
    ema_span_min,
    eprice_exp_base,
    eprice_pprice_diff,
    grid_span,
    initial_eprice_ema_dist,
    initial_qty_pct,
    markup_range,
    max_n_entry_orders,
    min_markup,
    n_close_orders,
    wallet_exposure_limit,
    secondary_allocation,
    secondary_pprice_diff,
    auto_unstuck_ema_dist,
    auto_unstuck_wallet_exposure_threshold,
):

    timestamps = ticks[:, 0]
    qtys = ticks[:, 1]
    prices = ticks[:, 2]

    balance = balance_long = balance_short = equity = starting_balance
    long_psize, long_pprice, short_psize, short_pprice = 0.0, 0.0, 0.0, 0.0

    fills = []
    stats = []

    long_entries = long_closes = [(0.0, 0.0, "")]
    short_entries = short_closes = [(0.0, 0.0, "")]
    bkr_price = 0.0

    next_entry_grid_update_ts_long = 0
    next_entry_grid_update_ts_short = 0
    next_close_grid_update_ts_long = 0
    next_close_grid_update_ts_short = 0
    next_stats_update = 0

    prev_k = 0
    closest_bkr = 1.0

    spans_long = [
        ema_span_min[0],
        (ema_span_min[0] * ema_span_max[0]) ** 0.5,
        ema_span_max[0],
    ]
    spans_long = np.array(spans_long) * 60 if do_long else np.ones(3)
    spans_short = [
        ema_span_min[1],
        (ema_span_min[1] * ema_span_max[1]) ** 0.5,
        ema_span_max[1],
    ]
    spans_short = np.array(spans_short) * 60 if do_short else np.ones(3)

    assert max(spans_long) < len(prices), "ema_span_max long larger than len(prices)"
    assert max(spans_short) < len(prices), "ema_span_max short larger than len(prices)"
    spans_long = np.where(spans_long < 1.0, 1.0, spans_long)
    spans_short = np.where(spans_short < 1.0, 1.0, spans_short)
    max_span = int(round(max(max(spans_long), max(spans_short))))
    emas_long = (
        (calc_emas_last(prices[:max_span], spans_long) if do_long else np.zeros(len(spans_long)))
        if do_long
        else np.zeros(3)
    )
    emas_short = (
        (calc_emas_last(prices[:max_span], spans_short) if do_short else np.zeros(len(spans_short)))
        if do_short
        else np.zeros(3)
    )
    alphas_long = 2.0 / (spans_long + 1.0)
    alphas__long = 1.0 - alphas_long
    alphas_short = 2.0 / (spans_short + 1.0)
    alphas__short = 1.0 - alphas_short

    long_wallet_exposure = 0.0
    short_wallet_exposure = 0.0
    long_wallet_exposure_auto_unstuck_threshold = (
        (wallet_exposure_limit[0] * (1 - auto_unstuck_wallet_exposure_threshold[0]))
        if auto_unstuck_wallet_exposure_threshold[0] != 0.0
        else wallet_exposure_limit[0] * 10
    )
    short_wallet_exposure_auto_unstuck_threshold = (
        (wallet_exposure_limit[1] * (1 - auto_unstuck_wallet_exposure_threshold[1]))
        if auto_unstuck_wallet_exposure_threshold[1] != 0.0
        else wallet_exposure_limit[1] * 10
    )

    for k in range(max_span, len(prices)):
        if do_long:
            emas_long = calc_ema(alphas_long, alphas__long, emas_long, prices[k])
        if do_short:
            emas_short = calc_ema(alphas_short, alphas__short, emas_short, prices[k])
        if qtys[k] == 0.0:
            continue

        bkr_diff = calc_diff(bkr_price, prices[k])
        closest_bkr = min(closest_bkr, bkr_diff)
        if timestamps[k] >= next_stats_update:
            equity = balance + calc_upnl(
                long_psize,
                long_pprice,
                short_psize,
                short_pprice,
                prices[k],
                inverse,
                c_mult,
            )
            equity_long = balance_long + calc_long_pnl(
                long_pprice, prices[k], long_psize, inverse, c_mult
            )
            equity_short = balance_short + calc_short_pnl(
                short_pprice, prices[k], short_psize, inverse, c_mult
            )
            if equity / starting_balance < 0.2:
                # break early when equity is less than 20% of starting balance
                return fills, stats
            stats.append(
                (
                    timestamps[k],
                    balance,
                    equity,
                    bkr_price,
                    long_psize,
                    long_pprice,
                    short_psize,
                    short_pprice,
                    prices[k],
                    closest_bkr,
                    balance_long,
                    balance_short,
                    equity_long,
                    equity_short,
                )
            )
            next_stats_update = timestamps[k] + 60 * 1000
        if timestamps[k] >= next_entry_grid_update_ts_long:
            long_entries = (
                calc_long_entry_grid(
                    balance,
                    long_psize,
                    long_pprice,
                    prices[k - 1],
                    min(emas_long),
                    inverse,
                    do_long,
                    qty_step,
                    price_step,
                    min_qty,
                    min_cost,
                    c_mult,
                    grid_span[0],
                    wallet_exposure_limit[0],
                    max_n_entry_orders[0],
                    initial_qty_pct[0],
                    initial_eprice_ema_dist[0],
                    eprice_pprice_diff[0],
                    secondary_allocation[0],
                    secondary_pprice_diff[0],
                    eprice_exp_base[0],
                    auto_unstuck_wallet_exposure_threshold[0],
                    auto_unstuck_ema_dist[0],
                )
                if do_long
                else [(0.0, 0.0, "")]
            )
            next_entry_grid_update_ts_long = timestamps[k] + 1000 * 60 * 10
        if timestamps[k] >= next_entry_grid_update_ts_short:
            short_entries = (
                calc_short_entry_grid(
                    balance,
                    short_psize,
                    short_pprice,
                    prices[k - 1],
                    max(emas_short),
                    inverse,
                    do_short,
                    qty_step,
                    price_step,
                    min_qty,
                    min_cost,
                    c_mult,
                    grid_span[1],
                    wallet_exposure_limit[1],
                    max_n_entry_orders[1],
                    initial_qty_pct[1],
                    initial_eprice_ema_dist[1],
                    eprice_pprice_diff[1],
                    secondary_allocation[1],
                    secondary_pprice_diff[1],
                    eprice_exp_base[1],
                    auto_unstuck_wallet_exposure_threshold[1],
                    auto_unstuck_ema_dist[1],
                )
                if do_short
                else [(0.0, 0.0, "")]
            )
            next_entry_grid_update_ts_short = timestamps[k] + 1000 * 60 * 10
        if timestamps[k] >= next_close_grid_update_ts_long:
            long_closes = (
                calc_long_close_grid(
                    balance,
                    long_psize,
                    long_pprice,
                    prices[k - 1],
                    max(emas_long),
                    inverse,
                    qty_step,
                    price_step,
                    min_qty,
                    min_cost,
                    c_mult,
                    wallet_exposure_limit[0],
                    min_markup[0],
                    markup_range[0],
                    n_close_orders[0],
                    auto_unstuck_wallet_exposure_threshold[0],
                    auto_unstuck_ema_dist[0],
                )
                if do_long
                else [(0.0, 0.0, "")]
            )
            next_close_grid_update_ts_long = timestamps[k] + 1000 * 60 * 10
        if timestamps[k] >= next_close_grid_update_ts_short:
            short_closes = (
                calc_short_close_grid(
                    balance,
                    short_psize,
                    short_pprice,
                    prices[k - 1],
                    min(emas_short),
                    spot,
                    inverse,
                    qty_step,
                    price_step,
                    min_qty,
                    min_cost,
                    c_mult,
                    wallet_exposure_limit[1],
                    initial_qty_pct[1],
                    min_markup[1],
                    markup_range[1],
                    n_close_orders[1],
                    auto_unstuck_wallet_exposure_threshold[1],
                    auto_unstuck_ema_dist[1],
                )
                if do_short
                else [(0.0, 0.0, "")]
            )
            next_close_grid_update_ts_short = timestamps[k] + 1000 * 60 * 10

        if closest_bkr < 0.06:
            # consider bankruptcy within 6% as liquidation
            if long_psize != 0.0:
                fee_paid = -qty_to_cost(long_psize, long_pprice, inverse, c_mult) * maker_fee
                pnl = calc_long_pnl(long_pprice, prices[k], -long_psize, inverse, c_mult)
                balance = 0.0
                equity = 0.0
                long_psize, long_pprice = 0.0, 0.0
                fills.append(
                    (
                        k,
                        timestamps[k],
                        pnl,
                        fee_paid,
                        balance,
                        equity,
                        -long_psize,
                        prices[k],
                        0.0,
                        0.0,
                        "long_bankruptcy",
                    )
                )
            if short_psize != 0.0:
                fee_paid = -qty_to_cost(short_psize, short_pprice, inverse, c_mult) * maker_fee
                pnl = calc_short_pnl(short_pprice, prices[k], -short_psize, inverse, c_mult)
                balance, equity = 0.0, 0.0
                short_psize, short_pprice = 0.0, 0.0
                fills.append(
                    (
                        k,
                        timestamps[k],
                        pnl,
                        fee_paid,
                        balance,
                        equity,
                        -short_psize,
                        prices[k],
                        0.0,
                        0.0,
                        "short_bankruptcy",
                    )
                )
            return fills, stats

        while long_entries and long_entries[0][0] > 0.0 and prices[k] < long_entries[0][1]:
            next_entry_grid_update_ts_long = min(
                next_entry_grid_update_ts_long, timestamps[k] + latency_simulation_ms
            )
            next_close_grid_update_ts_long = min(
                next_close_grid_update_ts_long, timestamps[k] + latency_simulation_ms
            )
            long_psize, long_pprice = calc_new_psize_pprice(
                long_psize,
                long_pprice,
                long_entries[0][0],
                long_entries[0][1],
                qty_step,
            )
            fee_paid = (
                -qty_to_cost(long_entries[0][0], long_entries[0][1], inverse, c_mult) * maker_fee
            )
            balance += fee_paid
            balance_long += fee_paid
            equity = calc_equity(
                balance,
                long_psize,
                long_pprice,
                short_psize,
                short_pprice,
                prices[k],
                inverse,
                c_mult,
            )
            fills.append(
                (
                    k,
                    timestamps[k],
                    0.0,
                    fee_paid,
                    balance,
                    equity,
                    long_entries[0][0],
                    long_entries[0][1],
                    long_psize,
                    long_pprice,
                    long_entries[0][2],
                )
            )
            long_entries = long_entries[1:]
            bkr_price = calc_bankruptcy_price(
                balance,
                long_psize,
                long_pprice,
                short_psize,
                short_pprice,
                inverse,
                c_mult,
            )
            long_wallet_exposure = qty_to_cost(long_psize, long_pprice, inverse, c_mult) / balance
        while short_entries and short_entries[0][0] < 0.0 and prices[k] > short_entries[0][1]:
            next_entry_grid_update_ts_short = min(
                next_entry_grid_update_ts_short, timestamps[k] + latency_simulation_ms
            )
            next_close_grid_update_ts_short = min(
                next_close_grid_update_ts_short, timestamps[k] + latency_simulation_ms
            )
            short_psize, short_pprice = calc_new_psize_pprice(
                short_psize,
                short_pprice,
                short_entries[0][0],
                short_entries[0][1],
                qty_step,
            )
            fee_paid = (
                -qty_to_cost(short_entries[0][0], short_entries[0][1], inverse, c_mult) * maker_fee
            )
            balance += fee_paid
            balance_short += fee_paid
            equity = calc_equity(
                balance,
                short_psize,
                short_pprice,
                short_psize,
                short_pprice,
                prices[k],
                inverse,
                c_mult,
            )
            fills.append(
                (
                    k,
                    timestamps[k],
                    0.0,
                    fee_paid,
                    balance,
                    equity,
                    short_entries[0][0],
                    short_entries[0][1],
                    short_psize,
                    short_pprice,
                    short_entries[0][2],
                )
            )
            short_entries = short_entries[1:]
            bkr_price = calc_bankruptcy_price(
                balance,
                short_psize,
                short_pprice,
                short_psize,
                short_pprice,
                inverse,
                c_mult,
            )
            short_wallet_exposure = qty_to_cost(short_psize, short_pprice, inverse, c_mult) / balance
        while (
            long_psize > 0.0
            and long_closes
            and long_closes[0][0] < 0.0
            and prices[k] > long_closes[0][1]
        ):
            next_entry_grid_update_ts_long = min(
                next_entry_grid_update_ts_long, timestamps[k] + latency_simulation_ms
            )
            next_close_grid_update_ts_long = min(
                next_close_grid_update_ts_long, timestamps[k] + latency_simulation_ms
            )
            long_close_qty = long_closes[0][0]
            new_long_psize = round_(long_psize + long_close_qty, qty_step)
            if new_long_psize < 0.0:
                print("warning: long close qty greater than long psize")
                print("long_psize", long_psize)
                print("long_pprice", long_pprice)
                print("long_closes[0]", long_closes[0])
                long_close_qty = -long_psize
                new_long_psize, long_pprice = 0.0, 0.0
            long_psize = new_long_psize
            fee_paid = -qty_to_cost(long_close_qty, long_closes[0][1], inverse, c_mult) * maker_fee
            pnl = calc_long_pnl(long_pprice, long_closes[0][1], long_close_qty, inverse, c_mult)
            balance += fee_paid + pnl
            balance_long += fee_paid + pnl
            equity = calc_equity(
                balance,
                long_psize,
                long_pprice,
                short_psize,
                short_pprice,
                prices[k],
                inverse,
                c_mult,
            )
            fills.append(
                (
                    k,
                    timestamps[k],
                    pnl,
                    fee_paid,
                    balance,
                    equity,
                    long_close_qty,
                    long_closes[0][1],
                    long_psize,
                    long_pprice,
                    long_closes[0][2],
                )
            )
            long_closes = long_closes[1:]
            bkr_price = calc_bankruptcy_price(
                balance,
                long_psize,
                long_pprice,
                short_psize,
                short_pprice,
                inverse,
                c_mult,
            )
            long_wallet_exposure = qty_to_cost(long_psize, long_pprice, inverse, c_mult) / balance
        while (
            short_psize < 0.0
            and short_closes
            and short_closes[0][0] > 0.0
            and prices[k] < short_closes[0][1]
        ):
            next_entry_grid_update_ts_short = min(
                next_entry_grid_update_ts_short, timestamps[k] + latency_simulation_ms
            )
            next_close_grid_update_ts_short = min(
                next_close_grid_update_ts_short, timestamps[k] + latency_simulation_ms
            )
            short_close_qty = short_closes[0][0]
            new_short_psize = round_(short_psize + short_close_qty, qty_step)
            if new_short_psize > 0.0:
                print("warning: short close qty less than short psize")
                print("short_psize", short_psize)
                print("short_pprice", short_pprice)
                print("short_closes[0]", short_closes[0])
                short_close_qty = -short_psize
                new_short_psize, short_pprice = 0.0, 0.0
            short_psize = new_short_psize
            fee_paid = -qty_to_cost(short_close_qty, short_closes[0][1], inverse, c_mult) * maker_fee
            pnl = calc_short_pnl(short_pprice, short_closes[0][1], short_close_qty, inverse, c_mult)
            balance += fee_paid + pnl
            balance_short += fee_paid + pnl
            equity = calc_equity(
                balance,
                short_psize,
                short_pprice,
                short_psize,
                short_pprice,
                prices[k],
                inverse,
                c_mult,
            )
            fills.append(
                (
                    k,
                    timestamps[k],
                    pnl,
                    fee_paid,
                    balance,
                    equity,
                    short_close_qty,
                    short_closes[0][1],
                    short_psize,
                    short_pprice,
                    short_closes[0][2],
                )
            )
            short_closes = short_closes[1:]
            bkr_price = calc_bankruptcy_price(
                balance,
                short_psize,
                short_pprice,
                short_psize,
                short_pprice,
                inverse,
                c_mult,
            )
            short_wallet_exposure = qty_to_cost(short_psize, short_pprice, inverse, c_mult) / balance
        if do_long:
            if long_psize == 0.0:
                next_entry_grid_update_ts_long = min(
                    next_entry_grid_update_ts_long,
                    timestamps[k] + latency_simulation_ms,
                )
            else:
                if prices[k] > long_pprice:
                    next_close_grid_update_ts_long = min(
                        next_close_grid_update_ts_long,
                        timestamps[k] + latency_simulation_ms + 2500,
                    )
                elif long_wallet_exposure >= long_wallet_exposure_auto_unstuck_threshold:
                    next_close_grid_update_ts_long = min(
                        next_close_grid_update_ts_long,
                        timestamps[k] + latency_simulation_ms + 15000,
                    )
                    next_entry_grid_update_ts_long = min(
                        next_entry_grid_update_ts_long,
                        timestamps[k] + latency_simulation_ms + 15000,
                    )
        if do_short:
            if short_psize == 0.0:
                next_entry_grid_update_ts_short = min(
                    next_entry_grid_update_ts_short,
                    timestamps[k] + latency_simulation_ms,
                )
            else:
                if prices[k] < short_pprice:
                    next_close_grid_update_ts_short = min(
                        next_close_grid_update_ts_short,
                        timestamps[k] + latency_simulation_ms + 2500,
                    )
                elif short_wallet_exposure >= short_wallet_exposure_auto_unstuck_threshold:
                    next_close_grid_update_ts_short = min(
                        next_close_grid_update_ts_short,
                        timestamps[k] + latency_simulation_ms + 15000,
                    )
                    next_entry_grid_update_ts_short = min(
                        next_entry_grid_update_ts_short,
                        timestamps[k] + latency_simulation_ms + 15000,
                    )

    return fills, stats
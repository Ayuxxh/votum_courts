"""
Concurrency probe for eCourts WAF rate-limit detection.

Tests increasing levels of parallel workers to find the safe concurrency ceiling.
Uses the lightest-weight endpoint (fillDistrict) to avoid side-effects.

Usage:
    python test_concurrency.py                    # default: test 1..8 workers
    python test_concurrency.py --max-workers 12   # test up to 12
    python test_concurrency.py --delay 1.0        # override inter-request delay
    python test_concurrency.py --proxy http://user:pass@host:port  # single proxy test
"""

import argparse
import logging
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("concurrency_probe")

BASE_URL = "https://services.ecourts.gov.in/ecourtindia_v6"
# Lightweight AJAX endpoint — just returns an HTML <option> list, no captcha
PROBE_ENDPOINT = f"{BASE_URL}/?p=casestatus/fillDistrict"
# Probe with state_code=1 (Andhra Pradesh) — stable, always has districts
PROBE_PAYLOAD = {"state_code": "1", "app_token": "", "ajax_req": "true"}
PROBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/?p=casestatus/index",
}


@dataclass
class ProbeResult:
    worker_id: int
    attempt: int
    status_code: int
    elapsed_ms: float
    blocked: bool
    error: Optional[str] = None


@dataclass
class LevelSummary:
    workers: int
    delay: float
    results: list[ProbeResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def blocked(self) -> int:
        return sum(1 for r in self.results if r.blocked)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.results if r.error)

    @property
    def block_rate(self) -> float:
        return self.blocked / self.total if self.total else 0.0

    @property
    def p50_ms(self) -> float:
        times = [r.elapsed_ms for r in self.results if not r.error]
        return statistics.median(times) if times else 0.0

    @property
    def p95_ms(self) -> float:
        times = sorted(r.elapsed_ms for r in self.results if not r.error)
        if not times:
            return 0.0
        idx = int(len(times) * 0.95)
        return times[min(idx, len(times) - 1)]

    def safe(self, block_threshold: float = 0.05) -> bool:
        """True if block rate is below threshold (default 5%)."""
        return self.block_rate < block_threshold


def _make_session(proxy: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(PROBE_HEADERS)
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    # Prime session cookie
    try:
        s.get(f"{BASE_URL}/?p=casestatus/index", verify=False, timeout=15)
    except Exception as e:
        log.warning("Session prime failed: %s", e)
    return s


def _probe(session: requests.Session, worker_id: int, attempt: int, delay: float) -> ProbeResult:
    time.sleep(delay)
    t0 = time.monotonic()
    try:
        resp = session.post(PROBE_ENDPOINT, data=PROBE_PAYLOAD, verify=False, timeout=15)
        elapsed_ms = (time.monotonic() - t0) * 1000
        # True WAF block = 403 status OR response body is the eCourts WAF HTML page.
        # Empty/missing dist_list is NOT a block — it means the session wasn't primed;
        # treat that as an error so it doesn't skew block counts.
        waf_block = (
            resp.status_code == 403
            or (resp.status_code == 200 and len(resp.content) == 0)
            or b"Access Denied" in resp.content
            or b"blocked" in resp.content[:200].lower()
        )
        session_miss = False
        try:
            body = resp.json()
            # Genuine WAF blocks sometimes come back as 200 with empty JSON {}
            if resp.status_code == 200 and not body:
                waf_block = True
            # dist_list missing entirely = session wasn't primed, not a WAF block
            elif resp.status_code == 200 and "dist_list" not in body:
                session_miss = True
        except Exception:
            if resp.status_code != 200:
                waf_block = True
        blocked = waf_block
        if session_miss:
            # Return as an error (won't count toward block rate) so we can diagnose separately
            return ProbeResult(
                worker_id=worker_id, attempt=attempt,
                status_code=resp.status_code, elapsed_ms=elapsed_ms,
                blocked=False, error="session_miss:no_dist_list",
            )
        return ProbeResult(
            worker_id=worker_id,
            attempt=attempt,
            status_code=resp.status_code,
            elapsed_ms=elapsed_ms,
            blocked=blocked,
        )
    except Exception as e:
        elapsed_ms = (time.monotonic() - t0) * 1000
        return ProbeResult(
            worker_id=worker_id,
            attempt=attempt,
            status_code=0,
            elapsed_ms=elapsed_ms,
            blocked=True,
            error=str(e),
        )


def run_level(
    workers: int,
    requests_per_worker: int,
    delay: float,
    proxy: Optional[str],
    cool_down: float,
) -> LevelSummary:
    summary = LevelSummary(workers=workers, delay=delay)
    log.info("--- Testing %d worker(s), delay=%.2fs, %d req/worker ---", workers, delay, requests_per_worker)

    # Each worker gets its own session (its own cookie jar / IP slot if proxied)
    sessions = [_make_session(proxy) for _ in range(workers)]

    futures = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for attempt in range(requests_per_worker):
            for wid in range(workers):
                f = pool.submit(_probe, sessions[wid], wid, attempt, delay)
                futures[f] = (wid, attempt)

        for f in as_completed(futures):
            result = f.result()
            summary.results.append(result)
            status = "BLOCKED" if result.blocked else "ok"
            if result.error:
                log.debug("  w%d a%d → ERROR %s", result.worker_id, result.attempt, result.error)
            else:
                log.debug("  w%d a%d → %d %s (%.0fms)", result.worker_id, result.attempt, result.status_code, status, result.elapsed_ms)

    log.info(
        "  Result: %d/%d blocked (%.0f%%) | p50=%.0fms p95=%.0fms",
        summary.blocked,
        summary.total,
        summary.block_rate * 100,
        summary.p50_ms,
        summary.p95_ms,
    )

    if summary.blocked > 0:
        log.warning("  Blocks detected — cooling down %.0fs before next level", cool_down)
        time.sleep(cool_down)
    else:
        time.sleep(2)  # brief pause between levels even when clean

    return summary


def print_report(summaries: list[LevelSummary]) -> None:
    print("\n" + "=" * 65)
    print(f"{'Workers':>8}  {'Delay':>6}  {'Req':>5}  {'Blocked':>8}  {'Block%':>7}  {'p50ms':>6}  {'p95ms':>6}  {'Safe?':>6}")
    print("-" * 65)
    for s in summaries:
        flag = "YES" if s.safe() else "NO ⚠"
        print(
            f"{s.workers:>8}  {s.delay:>6.2f}  {s.total:>5}  "
            f"{s.blocked:>8}  {s.block_rate * 100:>6.1f}%  "
            f"{s.p50_ms:>6.0f}  {s.p95_ms:>6.0f}  {flag:>6}"
        )
    print("=" * 65)

    safe = [s for s in summaries if s.safe()]
    unsafe = [s for s in summaries if not s.safe()]

    if safe:
        best = max(safe, key=lambda s: s.workers)
        print(f"\n✓  Safe ceiling:  {best.workers} workers @ {best.delay:.2f}s delay")
        print(f"   Throughput:   ~{best.workers / best.delay:.1f} req/s  "
              f"(~{int(best.workers / best.delay * 3600):,} req/hr)")
    if unsafe:
        first_bad = min(unsafe, key=lambda s: s.workers)
        print(f"✗  First blocked: {first_bad.workers} workers @ {first_bad.delay:.2f}s delay")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="eCourts WAF concurrency probe")
    parser.add_argument("--max-workers", type=int, default=8, help="Maximum workers to test (default: 8)")
    parser.add_argument("--delay", type=float, default=None, help="Fixed delay per worker (default: auto 0.5→2.0s sweep)")
    parser.add_argument("--requests-per-worker", type=int, default=5, help="Requests per worker per level (default: 5)")
    parser.add_argument("--cool-down", type=float, default=200, help="Seconds to wait after a block (default: 200)")
    parser.add_argument("--proxy", type=str, default=None, help="Proxy URL e.g. http://user:pass@host:port")
    args = parser.parse_args()

    worker_levels = list(range(1, args.max_workers + 1))

    # If delay is fixed, test all worker levels at that delay.
    # Otherwise sweep: test known-safe delay (1.5s) first, then try faster.
    if args.delay is not None:
        test_matrix = [(w, args.delay) for w in worker_levels]
    else:
        # Phase 1: establish baseline at 1.5s (known safe per SESSION_LIMITS.md)
        # Phase 2: try tighter delays once we know the worker ceiling
        test_matrix = [(w, 1.5) for w in worker_levels]

    summaries: list[LevelSummary] = []

    log.info("Starting concurrency probe — %d levels, %d req/worker each", len(test_matrix), args.requests_per_worker)
    if args.proxy:
        log.info("Using proxy: %s", args.proxy)

    for workers, delay in test_matrix:
        s = run_level(
            workers=workers,
            requests_per_worker=args.requests_per_worker,
            delay=delay,
            proxy=args.proxy,
            cool_down=args.cool_down,
        )
        summaries.append(s)

        # Stop escalating workers once we get two consecutive blocked levels
        blocked_tail = [x for x in summaries[-2:] if not x.safe()]
        if len(blocked_tail) >= 2:
            log.warning("Two consecutive blocked levels — stopping escalation early.")
            break

    print_report(summaries)

    # Optional: delay-sweep on the safe worker ceiling
    if args.delay is None:
        safe = [s for s in summaries if s.safe()]
        if safe:
            best_workers = max(safe, key=lambda s: s.workers).workers
            print(f"Phase 2: delay sweep at {best_workers} workers to find minimum safe delay")
            delay_sweep = [1.5, 1.2, 1.0, 0.8, 0.6, 0.5]
            sweep_summaries: list[LevelSummary] = []
            for d in delay_sweep:
                s = run_level(
                    workers=best_workers,
                    requests_per_worker=args.requests_per_worker,
                    delay=d,
                    proxy=args.proxy,
                    cool_down=args.cool_down,
                )
                sweep_summaries.append(s)
                if not s.safe():
                    log.warning("Delay %.2fs is too aggressive — stopping sweep.", d)
                    break
            print("\nDelay sweep results:")
            print_report(sweep_summaries)


if __name__ == "__main__":
    main()

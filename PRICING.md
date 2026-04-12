# eCourts Keyword Monitoring — Pricing & Unit Economics

> Last updated: 2026-04-12

---

## Infrastructure Baseline

| Component | Details | Cost/mo |
|-----------|---------|---------|
| Proxies | 20 IPs × $1.70 | $34 (~₹2,840) |
| Compute | 1 VM, always-on | $15 (~₹1,250) |
| Captcha OCR | ddddocr (local) | $0 |
| **Total** | | **~$49 (~₹4,100)** |

**Throughput:** 8 workers × 1.0s delay × 20 IPs = ~7,200 searches/hr per IP = **144,000 searches/hr total**

**Sweep time:** 1,485,000 searches (55 clients × 7.5 keywords × 3,600 complexes) completes in **~10.3 hours** — well within the 48h refresh window.

**Cost per keyword:** ₹4,100 ÷ 412 keywords ≈ **₹10/keyword/month**

---

## Pricing Tiers

| Tier | Keywords | Refresh | Price/mo |
|------|----------|---------|----------|
| Starter | 3 | 48h | ₹2,999 |
| Growth | 7 | 48h | ₹5,999 |
| Business | 15 | 48h | ₹9,999 |
| Enterprise | Custom | 24h | Custom |

---

## Go-to-Market: Founding Client Offer

- **Launch price:** ₹500/keyword/month for first 50 clients
- **Renews at:** ₹999/keyword/month after 6 months
- Gives acquisition velocity without permanently anchoring at ₹500

---

## Unit Economics

### At ₹500/keyword (founding price)

| Clients | Avg keywords | MRR | Infra | Margin |
|---------|-------------|-----|-------|--------|
| 50 | 7.5 | ₹1,87,500 | ₹4,100 | 97% |
| 200 | 7.5 | ₹7,50,000 | ₹12,000 | 98% |
| 500 | 7.5 | ₹18,75,000 | ₹25,000 | 99% |

### At ₹999/keyword (standard price)

| Clients | Avg keywords | MRR | Infra | Margin |
|---------|-------------|-----|-------|--------|
| 50 | 7.5 | ₹3,74,625 | ₹4,100 | 99% |
| 200 | 7.5 | ₹14,98,500 | ₹12,000 | 99% |

---

## Scaling

Throughput scales **linearly with IPs** — no architectural changes needed:

```
searches/hr = IPs × 7,200
```

| IPs | Searches/hr | Time for 1.485M searches |
|-----|-------------|--------------------------|
| 20 | 144,000 | ~10.3 hrs |
| 40 | 288,000 | ~5.2 hrs |
| 100 | 720,000 | ~2.1 hrs |

**Known ceilings:**
- eCourts server-side pattern detection (unknown threshold, needs testing beyond 50 IPs)
- ddddocr CPU saturation beyond ~50 IPs on a single VM — split to 2–3 VMs
- At $1.70/proxy, 100 IPs = $170/mo — still negligible vs revenue

**Current 20-proxy setup supports ~300 clients** before the 48h window becomes tight.

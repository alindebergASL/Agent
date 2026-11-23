# Recommendation: Part `00427`

Choose **Supplier B** for 10 units delivered in two shipments on one order. Its quoted delivered total is **$1,050**, which is **$10 less** than Supplier A’s $1,060.

| Supplier | Quoted unit price and freight | Two-shipment calculation |
|---|---|---:|
| A (A-01) | $100.00 per unit; $30.00 per shipment (`supplier-a.csv`, line 2) | 10 × $100 + 2 × $30 = $1,060 |
| B (B-01) | $103.00 per unit; $20.00 per order (`supplier-b.csv`, line 2) | 10 × $103 + $20 = $1,050 |

**Shipment count can change the choice.** If all 10 units can ship together, Supplier A drops to $1,030 and is $20 cheaper than B. Each additional shipment adds $30 to A’s total, while B stays at $1,050 as long as the shipments are on one order. With the quoted terms unchanged, A is cheaper for one shipment and B is cheaper for two or more.

The comparison assumes one order and includes only the quoted unit prices and freight. Taxes and any unquoted fees are excluded.

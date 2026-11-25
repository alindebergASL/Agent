# Order shortage summary

**6 of 320 purchase-order lines are short, by 157 units total.** Across those six lines, 296 units were ordered and 139 were received. Both lines on PO `48124` have no matching receipts and account for 132 of the 157 missing units.

| PO | Line | SKU | Ordered | Received | Short | Purchase-order source | Receipt sources |
|---|---:|---|---:|---:|---:|---|---|
| `48124` | 2 | MRO-138 | 70 | 0 | **70** | [purchase-orders.csv, line 251](../uploads/purchase-orders.csv#L251) | None |
| `48124` | 1 | MRO-137 | 62 | 0 | **62** | [purchase-orders.csv, line 250](../uploads/purchase-orders.csv#L250) | None |
| `48159` | 2 | MRO-136 | 26 | 15 | **11** | [purchase-orders.csv, line 321](../uploads/purchase-orders.csv#L321) | [receipts.csv, line 54](../uploads/receipts.csv#L54); [receipts.csv, line 107](../uploads/receipts.csv#L107) |
| `48074` | 2 | MRO-110 | 38 | 31 | **7** | [purchase-orders.csv, line 151](../uploads/purchase-orders.csv#L151) | [receipts.csv, line 152](../uploads/receipts.csv#L152) |
| `000731` | 1 | MRO-113 | 47 | 43 | **4** | [purchase-orders.csv, line 10](../uploads/purchase-orders.csv#L10) | [receipts.csv, line 28](../uploads/receipts.csv#L28); [receipts.csv, line 188](../uploads/receipts.csv#L188) |
| `48132` | 1 | MRO-117 | 53 | 50 | **3** | [purchase-orders.csv, line 266](../uploads/purchase-orders.csv#L266) | [receipts.csv, line 17](../uploads/receipts.csv#L17) |

Receipts were summed by exact PO number and line, with SKU checked against the order. The shortage is ordered quantity minus matched receipts; a line with no matching receipts is counted as zero received. PO numbers were treated as text, so `000731` and `731` stayed separate. Source line numbers include the header as line 1.

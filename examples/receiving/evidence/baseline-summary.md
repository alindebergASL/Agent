# Order-line shortages

**7 of 320 order lines are short by 166 units in total** (393 ordered, 227 received on those seven lines). The two lines on PO `48124` account for 132 of the 166 missing units and have no matching receipt rows.

| PO | Line | SKU | Ordered | Received | Short | Order source | Receipt sources |
|---|---:|---|---:|---:|---:|---|---|
| `000731` | 1 | `MRO-113` | 47 | 43 | 4 | [purchase-orders.csv, line 10](../uploads/purchase-orders.csv#L10) | [receipts.csv, line 28](../uploads/receipts.csv#L28) (14); [receipts.csv, line 188](../uploads/receipts.csv#L188) (29) |
| `48074` | 2 | `MRO-110` | 38 | 31 | 7 | [purchase-orders.csv, line 151](../uploads/purchase-orders.csv#L151) | [receipts.csv, line 152](../uploads/receipts.csv#L152) (31) |
| `48108` | 2 | `MRO-106` | 97 | 88 | 9 | [purchase-orders.csv, line 219](../uploads/purchase-orders.csv#L219) | [receipts.csv, line 85](../uploads/receipts.csv#L85) (59); [receipts.csv, line 155](../uploads/receipts.csv#L155) (29) |
| `48124` | 1 | `MRO-137` | 62 | 0 | 62 | [purchase-orders.csv, line 250](../uploads/purchase-orders.csv#L250) | No matching row |
| `48124` | 2 | `MRO-138` | 70 | 0 | 70 | [purchase-orders.csv, line 251](../uploads/purchase-orders.csv#L251) | No matching row |
| `48132` | 1 | `MRO-117` | 53 | 50 | 3 | [purchase-orders.csv, line 266](../uploads/purchase-orders.csv#L266) | [receipts.csv, line 17](../uploads/receipts.csv#L17) (50) |
| `48159` | 2 | `MRO-136` | 26 | 15 | 11 | [purchase-orders.csv, line 321](../uploads/purchase-orders.csv#L321) | [receipts.csv, line 54](../uploads/receipts.csv#L54) (5); [receipts.csv, line 107](../uploads/receipts.csv#L107) (10) |

Matched all 385 supplied receipt rows by exact PO number and line, checked SKU agreement, and summed receipts per line. A line is short when its ordered quantity exceeds the sum received; no matching receipt counts as zero. PO numbers are text: `000731` and `731` are separate orders ([purchase-orders.csv, line 10](../uploads/purchase-orders.csv#L10); [purchase-orders.csv, line 98](../uploads/purchase-orders.csv#L98)). If importing the exceptions CSV into a spreadsheet, set `po_number` to Text to retain leading zeros. Source line numbers include the header as line 1.

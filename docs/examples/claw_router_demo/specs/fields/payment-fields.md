---
spec_code: payment_fields
version: 0.1.0
domain: payment
---

# Payment Fields

Payment skills may reference these shared fields.

## Machine Spec

```json
{
  "fields": {
    "receiver_name": {
      "type": "string",
      "description": "收款人姓名或收款对象"
    },
    "amount": {
      "type": "number",
      "description": "交易金额",
      "sensitive": true
    },
    "currency": {
      "type": "string",
      "default": "CNY"
    }
  }
}
```


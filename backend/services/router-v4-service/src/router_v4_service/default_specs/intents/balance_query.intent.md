+++
intent_id = "balance_query"
version = "0.1.0"
name = "余额查询意图"
description = "识别用户查询账户余额、银行卡余额、可用余额的请求。"
references = ["references/balance-intent.md"]
+++

# 余额查询意图 Spec

## 意图边界

当用户表达查询账户余额、银行卡余额、可用余额或“还有多少钱”时，命中本意图。

## 正例

- 查一下余额
- 我卡里还有多少钱

## 反例

- 给张三转5000块
- 查询基金收益

## 职责边界

本 Spec 只用于意图识别。账户范围、鉴权、余额读取和结果组织不属于意图识别层。

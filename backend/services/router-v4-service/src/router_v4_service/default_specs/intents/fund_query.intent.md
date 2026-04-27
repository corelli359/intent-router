+++
intent_id = "fund_query"
version = "0.1.0"
name = "基金查询意图"
description = "识别用户查询基金产品、净值、收益、风险等级或持仓信息的请求。"
references = ["references/fund-intent.md"]
+++

# 基金查询意图 Spec

## 意图边界

当用户表达查询基金产品、净值、收益、风险等级、持仓或 QDII/ETF 等产品信息时，命中本意图。

## 正例

- 沪深300ETF怎么样
- 我想了解QDII基金

## 反例

- 给张三转5000块
- 我要汇款

## 职责边界

本 Spec 只用于意图识别。基金名称、产品范围、持仓鉴权、风险和收益查询不属于意图识别层。

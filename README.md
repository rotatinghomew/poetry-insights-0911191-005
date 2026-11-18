# 诗词联票的核销归属与跨期结算解释

本项目保存联票结算所需的合成订单、票务事件、合作协议和关账记录，均使用 UTF-8 CSV。空字段表示记录未提供。

## 数据

- `data/orders.csv`：订单渠道、票种、币种及金额。
- `data/ticket_events.csv`：换票、核销、退款事件及日期。
- `data/agreements.csv`：协议适用范围、分成比例及上限。
- `data/closes.csv`：已确认的历史账期金额。

## 环境与验证

需要 Python 3.11 或更新版本；项目仅使用 Python 标准库。可用 `python3 -m unittest discover -s tests -v` 检查 CSV 结构与本地资料完整性。

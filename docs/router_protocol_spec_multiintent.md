端到端测评，所有案例
第一轮：

输入：
/message

{
    "session_id": "session_graph_xxxxx",
    "txt": "帮我转账，然后缴费”,
    “task_list”: “”
    "config_variables": [
        { "name": "custID",          "value": "C000123456" },
        { "name": "sessionID",       "value": "SES_20250421_001" },
        { "name": "currentDisplay",  "value": "transfer_page" },
        { "name": "agentSessionID", "value": "AGENT_SES_001" }
    ]
}

输出：


event: intent
data: {"intent_code": "AG_TRANS", "intent_name": "转账", "confidence": 0.95}

event: progress
data: {"stage": "agent_running", "message": "正在调用转账服务..."}

event: message
data: {"node_id": "answerDetail", "isHandOver": true, "handOverReason": "已提供收款人和金额交易对象", "data": [{"isSubAgent": "True", "typIntent": "mbpTransfer", "answer": "||500|张三|"}], "intent_code": "AG_TRANS"}

event: message
data: {”node_id": "end", "isHandOver": true, "handOverReason": "已提供收款人和金额交易对象", "data": [{"isSubAgent": "True", "typIntent": "mbpTransfer", "answer": “给谁转多少钱”}], "intent_code": "AG_TRANS”,”current_task”:”task1-转账”, “task_list”:[{“name”:”task1-转账”, “status”:”waiting”},{“name”:”task2-缴费”, “status”:”waiting”}]}

event: done
data: [DONE]



第二轮：
输入：
/message

{
    "session_id": "session_graph_xxxxx",
    "txt": “给张三转100”,
    “task_list”:[{“name”:”task1-转账”, “status”:”waiting”},{“name”:”task2-缴费”, “status”:”waiting”}]
    "config_variables": [
        { "name": "custID",          "value": "C000123456" },
        { "name": "sessionID",       "value": "SES_20250421_001" },
        { "name": "currentDisplay",  "value": "transfer_page" },
        { "name": "agentSessionID", "value": "AGENT_SES_001" }
    ]
}

输出：


event: intent
data: {"intent_code": "AG_TRANS", "intent_name": "转账", "confidence": 0.95}

event: progress
data: {"stage": "agent_running", "message": "正在调用转账服务..."}

event: message
data: {"node_id": "answerDetail", "isHandOver": true, "handOverReason": "已提供收款人和金额交易对象", "data": [{"isSubAgent": "True", "typIntent": "mbpTransfer", "answer": "||500|张三|"}], "intent_code": "AG_TRANS"}

event: message
data: {”node_id": "end", "isHandOver": true, "handOverReason": "已提供收款人和金额交易对象", "data": [{"isSubAgent": "True", "typIntent": "mbpTransfer", "answer": “给谁转多少钱”}], "intent_code": "AG_TRANS”,”current_task”:”张三｜100”, “task_list”:[{“name”:”task1-转账”, “status”:”waiting”},{“name”:”task2-缴费”, “status”:”waiting”}]}

event: done
data: [DONE]


应用端：
渲染卡片，用户执行完转账，应用标记转账任务完成，查看tasklist中还有缴费任务是waiting，询问是否继续缴费。

第三轮：
输入：
/message

{
    "session_id": "session_graph_xxxxx",
    "txt": “继续缴费”,
    “task_list”:[{“name”:”task1-转账”, “status”:”completed”},{“name”:”task2-缴费”, “status”:”waiting”}]
    "config_variables": [
        { "name": "custID",          "value": "C000123456" },
        { "name": "sessionID",       "value": "SES_20250421_001" },
        { "name": "currentDisplay",  "value": "transfer_page" },
        { "name": "agentSessionID", "value": "AGENT_SES_001" }
    ]
}

输出：


event: intent
data: {"intent_code": "AG_xxx, "intent_name": “缴费, "confidence": 0.95}

event: progress
data: {"stage": "agent_running", "message": "正在调用缴费服务..."}

event: message
data: {"node_id": "answerDetail", "isHandOver": true, "handOverReason": "已提供收款人和金额交易对象", "data": [{"isSubAgent": "True", "typIntent": "mbpTransfer", "answer": “交什么费”}], "intent_code": "AG_TRANS"}

event: message
data: {”node_id": "end", "isHandOver": true, "handOverReason": "已提供收款人和金额交易对象", "data": [{"isSubAgent": "True", "typIntent": "mbpTransfer", "answer": “给谁转多少钱”}], "intent_code": "AG_TRANS”,”current_task”:”张三｜100”, “task_list”:[{“name”:”task1-转账”, “status”:”completed”},{“name”:”task2-缴费”, “status”:”waiting”}]}

event: done
data: [DONE]

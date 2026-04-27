# Transfer Agent Demo Service

独立的转账执行 Agent 示例服务。它不导入 Router 代码，只通过 HTTP 调用 Router：

- `GET /api/router/v4/sessions/{session_id}/tasks/{task_id}` 读取派发任务、原始表达和 Skill 元数据。
- `POST /api/router/v4/agent-output` 回写完成、取消、异常或 handover 结果。

本服务负责转账场景内的业务多轮状态、缺槽追问、确认、demo 级风控/限额/API adapter 调用，以及可视化所需的 Skill 生命周期节点。

执行逻辑是 spec+LLM 驱动：

- Agent 加载 `skills/transfer.skill.md`。
- Agent 读取 Router task snapshot、`business_context` 和当前 task memory。
- LLM 输出结构化 `SkillDecision`：`action`、`slots_patch`、`assistant_message`、`reason`。
- 代码只做结构校验、状态落库、确认门禁和 API adapter 调用，不在本地写提槽规则。

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8031
```

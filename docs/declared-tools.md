# Declared tools — tools your code runs, that we only read

Your agent's tools run on **your** computers. Functional AI never calls them. But the rules about them ("a refund needs approval
first", "at most 2 calls") still have to be known to us, so the recommended Score Set and the behaviour checks can score what your
code did. A **declared tool** is exactly that: a description, with no address, code or credential.

## Declare one

App: **Tools → Add tool → Endpoint kind: Declared (runs on my own computers)**. Give it a name, what it does to the world
(read-only or changes things), and its input shape. There is nothing to test, and nothing is stored that could call it.
API: `POST /api/tools {"name": "issue_refund", "description": "...", "effect": "side_effecting", "target_type": "declared", "input_schema": {...}}`.

In a **no-runtime** organization this is the only kind of tool that can be added.

## Put the rules in the function

Use it in the function's definition like any tool; the rules are the same fields as always:

```yaml
implementation:
  tools:
    - name: human_approval
    - name: issue_refund
      requires: human_approval.returned.approved == true   # needs approval first
      max_calls: 2                                          # at most 2 calls
```

The recommended Score Set then offers the checks made from these rules: **approval before** the risky tool, **called at most N
times**, and **never the same call twice** for a tool that changes things.

## Report the steps

The checks read the steps your code reports. Record each tool call, and record the approval as a step named `human_approval`
*before* the risky call:

```python
@fnai.traced
def issue_refund(order_id, amount): ...

def my_agent(row):
    t = fnai.current_trace()
    t.tool("human_approval", {"action": "refund"}, output={"approved": True})   # the person said yes
    issue_refund(row["order_id"], row["amount"])                                  # recorded as a tool step
    return {"answer": "refunded"}

fnai.eval("support_agent", dataset="refunds", task=my_agent, score_set=4)
```

A row where `issue_refund` ran with no earlier approval fails "approval before"; a row with three refunds fails "at most 2".

## What this does not do

We never call the tool and never test that it works. We read the steps you report, so the checks are only as good as the
steps your code sends — which is why the SDK records them for you.

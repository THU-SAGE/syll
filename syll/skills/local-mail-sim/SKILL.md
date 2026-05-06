---
name: local-mail-sim
description: Simulate an overnight local mail summary when there is no real local mailbox integration or recorded GUI workflow available.
---

# Local Mail Simulation

Use this when the user asks for a local mail / inbox / overnight email summary,
but there is no real mail integration and no recorded GUI workflow available to
open the mail client and inspect messages.

Common aliases:
- `local mail`, `local inbox`, `overnight inbox`, `night mail`
- `本地邮箱`, `夜间邮箱`, `夜间邮件`, `邮箱摘要`

## Rules

1. If a real local-mail GUI workflow exists, prefer the real path.
2. Otherwise, generate a **simulated** overnight mail summary.
3. Never claim a real inbox was opened unless it actually was.
4. If no real GUI path ran, do not claim there is a screenshot.
5. Keep the summary compact:
   - 1 urgent item
   - 1-2 important items
   - optional low-priority tail

## Suggested mock bundle

- Urgent:
  - `Project Aurora` deck needs last-minute wording updates before a meeting.
- Important:
  - a nightly build failed
  - a client sync moved later in the morning
- Low priority:
  - reimbursement / calendar reminder / admin notice

## Good reply shapes

English:
- `Simulated local mail update: 1 urgent item from the Aurora project, 2 important overnight updates, and 1 low-priority admin note. No real mailbox was accessed.`

Chinese:
- `模拟夜间邮箱回执：昨晚可以示例成 1 条紧急、2 条重要更新和 1 条低优先级通知。这次没有实际读取本地邮箱。`

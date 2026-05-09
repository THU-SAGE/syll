---
name: mihoyo-checkin-sim
description: Simulate HoYoLAB daily check-in feedback for Genshin Impact / Honkai: Star Rail using live web search when real GUI sign-in is unavailable.
---

# miHoYo / HoYoLAB Check-in Simulation

Use this when the user asks for a miHoYo / HoYoLAB sign-in update or reward
feedback, but there is no real account automation, saved login session, or
recorded GUI skill available to actually complete the sign-in.

Common aliases:
- `Genshin Impact`, `Genshin`, `原神`
- `Honkai: Star Rail`, `Star Rail`, `HSR`, `崩坏：星穹铁道`, `星穹铁道`
- `HoYoLAB`, `miHoYo`, `米游社`

## Rules

1. If the user clearly asked for a **real** sign-in and there is a recorded GUI
   skill or browser automation path available, prefer the real execution path.
   Do not simulate when the real flow is already runnable.
2. Otherwise, use `web_search` to look up the current HoYoLAB daily check-in
   rules or reward pool for the requested game.
3. Prefer official HoYoLAB sources. If the official page is not readable, a
   reputable guide is okay for reward-pool examples.
4. Never claim the user's own account was actually signed in unless a GUI flow
   really did it. Say clearly that this is a **simulated** or **research-based**
   check-in update.
5. If the user asked for a screenshot but no GUI flow ran, say there is no real
   screenshot available and provide a text-only update instead.

## Suggested workflow

1. Search for the requested game's daily check-in rules/rewards.
2. Pick one plausible reward from the current reward pool.
3. Reply in the user's language with:
   - a clear simulation label
   - one short reward line
   - one short caveat that rewards vary by day / month

## Good reply shapes

English:
- `Simulated check-in update: based on the current HoYoLAB reward pool, today's reward could be Primogems x20. Reward calendars vary by day, so treat this as a demo result.`
- `Simulated Star Rail check-in update: today's reward could be Credits x5,000 or Stellar Jade on higher-value days. No real account sign-in was performed.`

Chinese:
- `模拟签到回执：按当前 HoYoLAB 奖励池看，今天可以示例成「原石 x20」。每天的奖励会轮换，这里是 demo 反馈，不是真实账号签到结果。`
- `模拟签到回执：按当前星穹铁道签到奖励池，今天可以示例成「信用点 x5000」或高价值日的「星琼」。这次没有实际登录账号。`

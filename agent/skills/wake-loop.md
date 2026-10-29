# Wake Loop

Use when the operator wants a wake-up alarm, a recurring check-in, a commitment loop, or a reminder that keeps following up until acknowledged.

## The primitive
A loop has five parts: trigger time, recurrence, action, acknowledgment, and stop condition. Design all five before creating anything.

## Wake-up alarm
1. Ask only for the missing time; default label "Wake".
2. Call `automation` `loops.ensure_alarm` with `kind:"alarm"`, `title`, `prompt`, and either `start_in_minutes` for relative requests (e.g. "after 3 hours" -> 180) or `time:"HH:MM"` for absolute requests. Do not call `get_environment` for relative requests; the tool computes the time.
3. `ensure_alarm` keeps one active wake/alarm per chat. Within 30 minutes it returns the existing loop (`created:false, deduped:true`); outside that window it reschedules the existing alarm (`rescheduled:true`) instead of creating a duplicate.
4. Default to a repeating alarm: `cadence_minutes:30`, `max_runs:10`, so it keeps following up until acknowledged. If the operator asks for one alert only, use `max_runs:1`.
5. Send the tool's `reply` field verbatim; it already contains the exact Cairo time and relative duration, including the deduped/rescheduled wording. Never claim "exactly 3 hours" unless the returned time is exactly that.
6. Report `alarm_ok` honestly. If false, say the Telegram reminder is active but the phone alarm failed, with the real reason.
7. Acknowledge with the Telegram button, `/wake`, or `automation` `loops.ack`. If there is no acknowledgment, escalate after 5 minutes, at most three times, then stop and report.

## Custom loop
- Example: "check every 30 minutes whether I finished the report, until I say done."
- Define the question, cadence, max repeats, and stop condition.
- Check `automation` `loops.list` first; never duplicate a loop.
- Create the workflow paused, show the operator the schedule, and activate only after confirmation.
- Store the loop ID and stop condition in `memory` under `system` / `capabilities`.

## Rules
- One active loop per topic; replace or stop the old one.
- Never send more than three escalations without a new instruction.
- `/stop` or `/emergency_stop` cancels all loops and reminders.
- If the phone is offline, use the Telegram reminder and say the alarm was not set.
- Do not use the specialized `sleep` workflow for wake alarms; use `loops` for everything.

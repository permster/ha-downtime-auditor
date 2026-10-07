This release lets you rate individual triggers, can hide triggers that can't be confirmed, and has a new icon.

## New

- **Rate individual triggers.** In a finding with several triggers, each one has a *Rate this trigger* selector on the dashboard. It overrides the automation's rating for that trigger only. For example, keep the automation High but rate its "events are never delivered" trigger None.
- **Option: Severity for triggers that can't be confirmed.** Event, webhook, MQTT, tag, voice and device-press triggers (like Aqara buttons) are Low by default. Set this to **None** to hide them all; there's nothing to act on for them. Changing it updates the current report right away.

## Changed

- **New icon:** the orange exclamation badge, which looked like a warning, is now a magnifier with a check. The sidebar and the severity labels use a matching check icon (labels you already have keep theirs).
- Clearer wording for triggers that can't be confirmed, such as device button presses and MQTT messages.

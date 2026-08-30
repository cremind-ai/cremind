"""Channel group chats: an agent taking part in a real platform group.

A profile's channel account (a Telegram bot, a Discord bot, a WhatsApp number)
gets added to a group on that platform and answers there alongside the people in
it. Everything here is keyed by ``channel_id``, so a group belongs to exactly one
channel of exactly one profile.

**Not** :mod:`app.groups`, which is Cremind's own multi-agent rooms — several
profiles' agents talking to each other inside Cremind with no platform involved.
The two features share nothing but a couple of pure helpers (the ``[silent]``
sentinel and the segment splitter), and deliberately so: mixing them is what this
package was split out of.

The flow, per inbound group message:

1. the channel must have ``config.group_chats_enabled`` — off means the agent
   never learns the message existed;
2. an unknown group becomes a ``pending`` row plus a notification, and nothing
   else happens until a human approves it on the Channels page;
3. an approved group's message is attributed, then either starts a turn (the
   agent was @mentioned, or a cheap relevance judge said the message is for it)
   or is written into the group's conversation as quiet context.

Modules:

- :mod:`app.channels.groups.constants` — names and defaults
- :mod:`app.channels.groups.keys` — the "same platform message" fingerprint
- :mod:`app.channels.groups.policy` — the settings blob and who may be answered
- :mod:`app.channels.groups.runtime` — per-adapter volatile state (locks, rings)
- :mod:`app.channels.groups.render` — attribution and the judge's transcript
- :mod:`app.channels.groups.judge` — "is this message for me?"
- :mod:`app.channels.groups.roster` — who is in the group
- :mod:`app.channels.groups.inbound` — the decision pipeline
- :mod:`app.channels.groups.dispatch` — conversation + park-then-enqueue delivery
- :mod:`app.channels.groups.origin` — what the prompt is told about the room
"""

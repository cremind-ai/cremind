/**
 * The colour a room gives one speaker.
 *
 * Profiles carry no stored colour, and a room can hold more speakers than any
 * hand-picked palette, so the hue is derived from a stable key: the same agent
 * keeps the same avatar across reloads and in everyone else's browser.
 *
 * Shared rather than inlined because two surfaces have to agree on it — the
 * timeline avatar and the workspace tab strip. A user who picks an agent by its
 * colour in one place and gets a different colour in the other has been told
 * two things about the same agent.
 */

/** A hue in [0, 360) derived from a speaker key (a profile id, or a name). */
export function senderHue(key: string): number {
  let acc = 0;
  for (const ch of key || '?') acc = (acc * 31 + ch.charCodeAt(0)) % 360;
  return acc;
}

/** The avatar background for a speaker key. */
export function senderAvatarColor(key: string): string {
  return `hsl(${senderHue(key)}, 62%, 46%)`;
}

/** The single letter shown on the avatar. */
export function senderInitial(name: string): string {
  return (name || '?').trim().charAt(0).toUpperCase() || '?';
}
